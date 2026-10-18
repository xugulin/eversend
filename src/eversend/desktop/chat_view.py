"""The 会话 page: 消息在中间、输入区在底部。

Design notes
------------
* **One place decides what a message looks like.**  Every message -- typed on
  this computer, typed on a phone, or arrived from another computer -- is stored
  in :class:`~eversend.core.chat.ChatStore` first.  The view renders the store,
  so the three transports cannot drift apart in the UI.
* **The device list lives in the sidebar.**  This widget used to carry its own
  conversation list on the left; the sidebar's device list is that list now
  (see ``docs/UI_LAYOUT.md``), so a conversation is opened by clicking a device
  and this view only draws the open one.
* **Attachments are files, not BLOBs.**  A bubble shows a preview (image), a
  card (file/video) or a voice note, and clicking "打开" hands the path to the
  desktop's own opener.  The desktop build has no QtMultimedia (it is in
  PySide6-Addons, not the bundled Essentials), so playing audio *inside* the
  window is not possible -- saying that honestly beats a play button that does
  nothing.
* **Sending happens off the GUI thread.**  ``Engine.send_chat`` opens sockets and
  can block for seconds; running it inline would freeze the window.
"""

from __future__ import annotations

import html
import json
import os
import sys
import re
import subprocess
import threading
import time
from typing import Any, Iterable

from PySide6.QtCore import QPoint, QSize, Qt, QTimer, Signal, Slot
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from ..core import media, platform_open
from ..core.chat import (
    MEDIA_KINDS,
    client_aliases,
    conversation_people,
    conversation_title,
    direct_conversation_id,
)
from ..core.emoji import EMOJI_GROUPS
from ..core.engine import Engine
from ..core.model import human_bytes, human_speed
from . import icons, theme
from .preview import FilePreviewDialog, ImageViewer  # noqa: F401 - ImageViewer 是旧名字
from .widgets import (
    AttachmentBar,
    badge_pixmap,
    file_actions_menu,
    notify,
    overflow_menu_button,
)

try:  # 进度行（文字 + 细进度条）由 desktop-chat 提供；拿不到就退回本文件的 QLabel
    from .widgets import TransferProgress
except Exception:  # pragma: no cover - 只在 widgets 还没导出它时走到
    TransferProgress = None  # type: ignore[assignment]

#: The palette itself lives in the core so all three clients offer the same
#: set (see :mod:`eversend.core.emoji`); this module only draws it.
EMOJI = [emoji for _title, group in EMOJI_GROUPS for emoji in group]

#: 「待发送队列」里那些 state —— 消息进了队列（对方不在线 / 还没有 ACK）时，
#: 气泡右下角挂一个时钟角标，悬停写「等待对方上线」，并给出重试 / 放弃
#: （宪章 §3.3、§5.5）。核心那边用的是 ``queued``，其余几个是别名，防止
#: 值换名字之后界面直接不显示队列态。
QUEUED_STATES = ("queued", "pending", "waiting", "unsent")

#: 「今天 / 昨天 / 更早」—— 会话按日期分组（宪章 §3.3）。
DATE_GROUPS = ("今天", "昨天", "更早")


class EmojiPad(QFrame):
    """输入区里那块表情面板：宽度和输入框那一行一样，紧贴待发文件那一行的上边。

    以前它是一个 420×520 的独立窗口，点 😊 弹出来、飘在屏幕中间 —— 用户的要求是
    "表情的框显示宽度和对话框一致，紧贴文件预览那行的上边"，所以改成输入区里的
    一块内嵌面板：宽度跟着输入区走（两边对齐），位置在 `[表情面板] → [待发文件 +
    统计] → [输入行]` 这个顺序里的最上面。

    点一个表情就插进输入框，面板**不关**（可以连着挑几个）；面板有高度上限，
    超出就在面板里滚动，不把会话区挤没。
    """

    picked = Signal(str)

    #: 一个表情格子的宽度（按钮 + 间距），用来算一行放几个。按钮本身 44×42
    #: （尺寸写在 theme.py 的样式表里），格子留一点余量。
    CELL = 50

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("EmojiPad")
        self._columns = 0

        layout = QVBoxLayout(self)
        layout.setContentsMargins(
            theme.SPACE["sm"], theme.SPACE["sm"], theme.SPACE["sm"], theme.SPACE["sm"]
        )
        layout.setSpacing(theme.SPACE["xs"])

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.scroll.setObjectName("EmojiScroll")
        self.body = QWidget()
        self.body_layout = QVBoxLayout(self.body)
        self.body_layout.setContentsMargins(0, 0, 0, 0)
        self.body_layout.setSpacing(theme.SPACE["sm"])
        self.scroll.setWidget(self.body)
        layout.addWidget(self.scroll, 1)

        self.setMaximumHeight(300)
        self.setMinimumHeight(196)
        self.setVisible(False)

    def _build(self, columns: int) -> None:
        """按当前列数重建网格（列数只在宽度变化时变，平时不重建）。"""
        while self.body_layout.count():
            item = self.body_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.hide()
                widget.setParent(None)
                widget.deleteLater()
            elif item.layout() is not None:
                self._clear_layout(item.layout())
        for title, group in EMOJI_GROUPS:
            header = QLabel(f"{title}（{len(group)}）")
            header.setObjectName("Subtitle")
            self.body_layout.addWidget(header)
            row = None
            for index, emoji in enumerate(group):
                if index % columns == 0:
                    row = QHBoxLayout()
                    row.setSpacing(theme.SPACE["xs"])
                    self.body_layout.addLayout(row)
                button = QPushButton(emoji)
                # 尺寸由样式表给（见 theme.py 里 EmojiPad 那条注释），这里只
                # 声明希望它至少能被点得到。
                button.setMinimumSize(28, 28)
                button.setFlat(True)
                button.setCursor(Qt.PointingHandCursor)
                button.setToolTip(emoji)
                button.clicked.connect(lambda _=False, e=emoji: self.picked.emit(e))
                row.addWidget(button)
            if row is not None:
                row.addStretch(1)
        self.body_layout.addStretch(1)

    def _clear_layout(self, layout) -> None:
        while layout.count():
            item = layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.hide()
                widget.setParent(None)
                widget.deleteLater()
            elif item.layout() is not None:
                self._clear_layout(item.layout())

    def _fit(self) -> None:
        width = max(self.CELL, self.width() - 24)
        columns = max(4, min(24, width // self.CELL))
        if columns != self._columns:
            self._columns = columns
            self._build(columns)

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt naming
        super().resizeEvent(event)
        self._fit()

    def showEvent(self, event) -> None:  # noqa: N802 - Qt naming
        super().showEvent(event)
        self._fit()

    def toggle(self) -> bool:
        """展开/收起，返回展开后是否可见。"""
        visible = not self.isVisible()
        self.setVisible(visible)
        if visible:
            self._fit()
        return visible


#: 一段 emoji 的取法：区域指示符（国旗成对）、符号与象形、表情、交通、补充符号，
#: 外加零宽连接符 / 变体选择符 / 肤色修饰符 —— 这样 👨‍👩‍👧、👋🏻、🇨🇳 都是一个整体，
#: 不会在中间被切开（切开了就会变成几个"半个"的表情）。
EMOJI_RANGES = (
    "\U0001F1E6-\U0001F1FF"
    "\U0001F300-\U0001F5FF"
    "\U0001F600-\U0001F64F"
    "\U0001F680-\U0001F6FF"
    "\U0001F900-\U0001F9FF"
    "\U0001FA00-\U0001FAFF"
    "\u2600-\u27BF"
    "\u2B00-\u2BFF"
    "\uFE0F\u200D\U0001F3FB-\U0001F3FF"
)
EMOJI_RE = re.compile(f"[{EMOJI_RANGES}]+")

#: 消息里 emoji 的字号。和表情面板一样大 —— 用户在面板里挑的时候看到的是这个
#: 尺寸，发出去就缩成正文大小会显得"选的和发的不是一个东西"（安卓那头同样处理）。
EMOJI_FONT_PX = 26


def rich_with_emoji(text: str) -> str | None:
    """把 emoji 段包成更大的字号，返回富文本；没有 emoji 时返回 None。

    返回 None 让调用方继续用纯文本：绝大多数消息没有 emoji，没必要为它们
    走富文本（纯文本还能被测试和复制按原样读到）。
    """
    plain = str(text or "")
    if not plain or not EMOJI_RE.search(plain):
        return None
    parts: list[str] = []
    position = 0
    for match in EMOJI_RE.finditer(plain):
        parts.append(html.escape(plain[position : match.start()]).replace("\n", "<br>"))
        parts.append(
            f'<span style="font-size:{EMOJI_FONT_PX}px;">{html.escape(match.group(0))}</span>'
        )
        position = match.end()
    parts.append(html.escape(plain[position:]).replace("\n", "<br>"))
    return "".join(parts)


def emoji_label(text: str, **kwargs) -> QLabel:
    """一个会"把 emoji 放大"的 QLabel（没有 emoji 时就是普通标签）。"""
    label = QLabel(str(text or ""))
    rich = rich_with_emoji(text)
    if rich is not None:
        label.setTextFormat(Qt.RichText)
        label.setText(rich)
    label.setWordWrap(True)
    for key, value in kwargs.items():
        getattr(label, f"set{key[0].upper()}{key[1:]}")(value)
    return label


class MessageBubble(QFrame):
    """一个消息气泡：自己发的靠右（强调底）、对方发的靠左（面底 + 描边）。

    额外能挂一个**右下角的角标** —— 现在只有一个用途：消息还在"待发送"队列里
    时挂一个时钟（悬停「等待对方上线」，宪章 §3.3）。角标是绝对定位的子控件，
    所以气泡大小一变就要重新摆一次。
    """

    def __init__(self, outgoing: bool, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("BubbleOut" if outgoing else "BubbleIn")
        self.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Preferred)
        self.corner_badge: QLabel | None = None

    def set_corner_badge(self, badge: QLabel) -> None:
        badge.setParent(self)
        badge.raise_()
        self.corner_badge = badge
        self._place_badge()

    def _place_badge(self) -> None:
        if self.corner_badge is None:
            return
        self.corner_badge.move(
            max(0, self.width() - self.corner_badge.width() - 6),
            max(0, self.height() - self.corner_badge.height() - 6),
        )

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt naming
        super().resizeEvent(event)
        self._place_badge()

    def showEvent(self, event) -> None:  # noqa: N802 - Qt naming
        super().showEvent(event)
        self._place_badge()


class ChatView(QWidget):
    """The open conversation: 消息 + 输入区（会话列表在侧栏里）。"""

    #: Emitted when a send finished (ok, error) so the window can show it.
    send_finished = Signal(bool, str)
    #: 用户按了「发送」（输入框回车也算）—— 发文字还是发文件由窗口决定。
    send_requested = Signal()
    #: 用户按了「传输文件」（📎）—— 选文件的事窗口来做。
    attach_requested = Signal()
    #: 拖进窗口的文件。
    paths_dropped = Signal(list)
    #: 要预览哪个文件（气泡上的「预览」按钮、待发方块都发这个）。
    preview_requested = Signal(str)
    #: 会话列表变了（侧栏据此更新未读/置顶/最后一条）。
    conversations_changed = Signal(list)
    #: 当前打开的是哪个会话。
    conversation_changed = Signal(str)

    def __init__(self, engine: Engine, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.engine = engine
        self._conversation = ""
        #: 画布上现在画的是哪些消息、各自什么状态（``[(id, state)]``）。
        self._rendered: list[tuple[str, str]] = []
        #: 画布上现在画的是哪段会话。只比较消息 id 是不够的：新打开的会话一条
        #: 消息都没有时，"没有消息"和"还没画过"长得一模一样，画布就被跳过，
        #: 上一段会话的气泡留在原地 —— 用户看到的是"换了一段会话，气泡还是旧的"。
        self._canvas_conv = ""
        self._busy = False
        #: (气泡, 消息) —— 每次刷新时把传输进度写进气泡里
        self._media_bubbles: list[tuple[QWidget, dict[str, Any]]] = []
        #: 传输失败的原因（``callable(message_id) -> str``）。手机/文件传输那条路
        #: 不在聊天投递队列里，store 里也没有 reason 这一列，所以原因由窗口交进来
        #: （见 ``_failed_text``）。
        self._failure_reason_provider = None
        #: 上一次广播出去的会话签名，避免每 1.5 秒重建一次侧栏。
        self._conversation_sig: tuple = ()
        self._build()
        self._tick = QTimer(self)
        self._tick.setInterval(1500)
        self._tick.timeout.connect(self._on_tick)
        self._tick.start()
        self.reload()

    # -- construction ------------------------------------------------------

    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ---- 中间：消息 ----
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setObjectName("ChatScroll")
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.messages_host = QWidget()
        self.messages_layout = QVBoxLayout(self.messages_host)
        self.messages_layout.setContentsMargins(
            theme.SPACE["lg"], theme.SPACE["md"], theme.SPACE["lg"], theme.SPACE["md"]
        )
        self.messages_layout.setSpacing(theme.SPACE["sm"])
        self.messages_layout.addStretch(1)
        self.scroll.setWidget(self.messages_host)
        root.addWidget(self.scroll, 1)

        # ---- 底部：输入区 ----
        # 待发送队列的提示条（只有真的排着队时才出现，平时完全不占位）。
        self.queue_banner = self._build_queue_banner()
        root.addWidget(self.queue_banner)

        composer = QFrame()
        composer.setObjectName("Composer")
        outer = QVBoxLayout(composer)
        outer.setContentsMargins(
            theme.SPACE["lg"], theme.SPACE["sm"], theme.SPACE["lg"], theme.SPACE["md"]
        )
        outer.setSpacing(theme.SPACE["sm"])

        # 表情面板在最上面（宽度跟着输入区走），下面才是"要发什么"和输入行。
        self.emoji_pad = EmojiPad()
        self.emoji_pad.picked.connect(self._insert_emoji)
        outer.addWidget(self.emoji_pad)

        # 上面那一行是"要发什么"：缩略图方块 + 「共 N 项 · X MB」统计。
        self.attachment_bar = AttachmentBar(self.engine.config.data_dir)
        self.attachment_bar.paths_dropped.connect(self.paths_dropped)
        self.attachment_bar.preview_requested.connect(self.preview_requested)
        #: 待发文件那一行右边的统计（窗口按旧名字 total_label 读它）。
        self.total_label = self.attachment_bar.stats_label
        outer.addWidget(self.attachment_bar)

        # 最后一行才是输入框和按钮（上面那一行是"要发什么"，见
        # docs/UI_LAYOUT.md §3⑥：统计那行在输入框上面，输入框贴着底）。
        self.composer_box = QFrame()
        self.composer_box.setObjectName("ComposerBox")
        row = QHBoxLayout(self.composer_box)
        row.setContentsMargins(
            theme.SPACE["sm"], theme.SPACE["xs"], theme.SPACE["sm"], theme.SPACE["xs"]
        )
        row.setSpacing(theme.SPACE["sm"])

        # 「传输文件」是**唯一**的选文件入口（侧栏不再有"发送文件"）。
        self.attach_button = QPushButton("  传输文件")
        self.attach_button.setToolTip("选择要发送的文件或文件夹（也可以直接拖进来）")
        self.attach_button.setIcon(icons.icon("paperclip", theme.current_text_color(), 16))
        self.attach_button.clicked.connect(self.attach_requested)
        row.addWidget(self.attach_button)

        self.emoji_button = QPushButton()
        self.emoji_button.setObjectName("IconButton")
        self.emoji_button.setCheckable(True)
        self.emoji_button.setToolTip("表情（面板就在输入框上面）")
        self.emoji_button.setIcon(icons.icon("smile", theme.current_text_color(), 17))
        self.emoji_button.setIconSize(QSize(17, 17))
        self.emoji_button.setFixedWidth(34)
        self.emoji_button.clicked.connect(self._toggle_emoji_pad)
        row.addWidget(self.emoji_button)

        self.input = QLineEdit()
        self.input.setObjectName("ComposerInput")
        self.input.setPlaceholderText("输入消息，回车发送")
        self.input.returnPressed.connect(self.send_requested)
        row.addWidget(self.input, 1)

        # PIN 只在**对方要求 PIN** 时才出现（PeerHeader 上有「需要 PIN」药丸）；
        # 平时这一格完全不占位 —— 常驻一个 PIN 输入框会让人以为"必须填"。
        self.pin_edit = QLineEdit()
        self.pin_edit.setObjectName("PinEdit")
        self.pin_edit.setPlaceholderText("PIN")
        self.pin_edit.setMaxLength(32)
        self.pin_edit.setFixedWidth(110)
        self.pin_edit.setToolTip("对方设置了接收 PIN：这里要填一样的")
        self.pin_edit.setVisible(False)
        self._pin_required = False
        row.addWidget(self.pin_edit)

        self.send_button = QPushButton("发送")
        self.send_button.setObjectName("Primary")
        self.send_button.setMinimumWidth(84)
        self.send_button.clicked.connect(self.send_requested)
        row.addWidget(self.send_button)
        outer.addWidget(self.composer_box)

        root.addWidget(composer)

    def _build_queue_banner(self) -> QWidget:
        """「N 条消息还在等对方上线」那一行（宪章 §5.5：队列要看得见）。

        每条待发消息在自己气泡里也有时钟角标和「立即重试 / 放弃」；这一条是
        "一眼看到还有几件没发出去"的总览，没有排队时整条隐藏。
        """
        banner = QFrame()
        banner.setObjectName("QueueBanner")
        row = QHBoxLayout(banner)
        row.setContentsMargins(
            theme.SPACE["lg"], theme.SPACE["sm"], theme.SPACE["lg"], 0
        )
        row.setSpacing(theme.SPACE["sm"])
        mark = QLabel()
        mark.setPixmap(badge_pixmap("clock", theme.WARNING, 14))
        mark.setFixedSize(14, 14)
        row.addWidget(mark)
        self.queue_label = QLabel("")
        self.queue_label.setObjectName("Subtitle")
        row.addWidget(self.queue_label)
        self.queue_retry_button = QPushButton("立即重试全部")
        self.queue_retry_button.setObjectName("Ghost")
        self.queue_retry_button.setToolTip("不等对方上线，马上再投一次队列里的消息")
        self.queue_retry_button.clicked.connect(self._retry_all_queued)
        row.addWidget(self.queue_retry_button)
        row.addStretch(1)
        banner.setVisible(False)
        return banner

    # -- 待发送队列（对面不在线时消息排队等它上线） --------------------------

    def queued_messages(self) -> list[dict[str, Any]]:
        """当前会话里还在队列里的消息。"""
        if not self._conversation:
            return []
        try:
            messages = self.engine.chat.messages(self._conversation, limit=200)
        except Exception:
            return []
        return [
            message
            for message in messages
            if message.get("direction") == "out"
            and str(message.get("state") or "") in QUEUED_STATES
        ]

    def _refresh_queue_banner(self) -> None:
        queued = self.queued_messages()
        if not queued:
            self.queue_banner.setVisible(False)
            return
        count = len(queued)
        self.queue_label.setText(
            f"{count} 条消息还在等对方上线，上线后会自动发出。"
        )
        self.queue_banner.setVisible(True)

    def _retry_all_queued(self) -> None:
        retried = 0
        for message in self.queued_messages():
            if self._retry_message(message, quiet=True):
                retried += 1
        self.status_message(
            f"已重新投递 {retried} 条消息" if retried else "队列里的消息没能重投（对方还是不在线）"
        )
        self.reload()

    def _retry_message(self, message: dict[str, Any], quiet: bool = False) -> bool:
        """立即重试一条待发消息。

        两条路（宪章 §5.5 的"可手动重试"）：
        1. 还在队列里 → 让核心 ``retry_message`` 立刻再投一次（不等退避到点）；
        2. 已经出了队列（重试用尽 / 从来没发现过对端）→ 界面按"重新发送"再投一次，
           内容还在手上（文字在消息里、附件在本机）。只回一句"不在队列里"对用户
           没有用。
        """
        message_id = str(message.get("id") or "")
        engine = self.engine
        action = getattr(engine, "retry_message", None) or getattr(engine, "retry_chat", None)
        if action is not None:
            try:
                ok = bool(action(message_id))
            except Exception as exc:
                self.status_message(f"重发失败：{exc}")
                return False
            if ok:
                if not quiet:
                    self.status_message("已重新投递，对方上线后就能收到")
                return True
        return self._resend_message(message, quiet=quiet)

    def _resend_message(self, message: dict[str, Any], quiet: bool = False) -> bool:
        """按原样再发一次这条消息（文字或附件）。"""
        conv = self._conversation
        if not conv:
            if not quiet:
                self.status_message("没有打开的会话，重发不了")
            return False
        source = str(message.get("mediaSource") or "")
        if source and os.path.isfile(source):
            self.send_attachment(source)
            return True
        text = str(message.get("text") or "").strip()
        if text:
            self._send(conv, "text", text)
            return True
        if not quiet:
            self.status_message("这条消息没有可以重发的内容（附件已经不在这台机器上了）")
        return False

    def _drop_message(self, message: dict[str, Any]) -> None:
        """放弃一条待发消息：不再重试，气泡上标成失败。"""
        message_id = str(message.get("id") or "")
        engine = self.engine
        action = getattr(engine, "drop_message", None) or getattr(engine, "discard_message", None)
        dropped = False
        try:
            if action is not None:
                dropped = bool(action(message_id))
            else:
                engine.chat.set_state(message_id, "failed")
                dropped = True
        except Exception as exc:
            self.status_message(f"放弃失败：{exc}")
            return
        self.status_message(
            "已放弃这条待发消息，不会再重试" if dropped else "这条消息已经不在重发队列里了"
        )
        self.reload()

    def status_message(self, text: str) -> None:
        """把一句话送到窗口底部状态栏（没有窗口时退回浮层提示）。"""
        notify(self, text)

    # -- PIN ---------------------------------------------------------------

    def set_pin_required(self, required: bool, reason: str = "") -> None:
        """对方要求 PIN 时把 PIN 输入框放出来，其它时候它不占位（宪章 §3.1）。

        ``window.pin_edit`` 仍然指向同一个控件，窗口发文件时读它的 ``text()``。
        """
        self._pin_required = bool(required)
        if self._pin_required:
            self.pin_edit.setToolTip(reason or "对方要求 PIN：这里要填和对方一样的 PIN")
        self._refresh_pin_field()

    def pin_required(self) -> bool:
        return self._pin_required

    def _peer_needs_pin(self) -> bool:
        """从当前会话的成员身上判断"对面要不要 PIN"（拿不到就当作不要）。

        这是一层兜底：窗口（主区顶部）知道得更准，它调 ``set_pin_required``。
        """
        if not self._conversation:
            return False
        try:
            conversation = self.engine.chat.conversation(self._conversation) or {}
            members = [str(m) for m in (conversation.get("members") or []) if str(m)]
            peers = {peer.info.device_id: peer for peer in self.engine.devices()}
        except Exception:
            return False
        for member in members:
            if member == self.engine.info.device_id:
                continue
            peer = peers.get(member)
            if peer is None:
                continue
            caps = peer.info.capabilities or {}
            if str(caps.get("pin") or "").strip() or caps.get("needPin"):
                return True
        return False

    def _refresh_pin_field(self) -> None:
        needed = self._pin_required or self._peer_needs_pin()
        self.pin_edit.setVisible(needed)
        if needed and not self.pin_edit.toolTip():
            self.pin_edit.setToolTip("对方要求 PIN：这里要填和对方一样的 PIN")

    # -- data --------------------------------------------------------------

    def conversations(self) -> list[dict[str, Any]]:
        return self.engine.chat.conversations()

    def conversation_rows(self) -> list[dict[str, Any]]:
        """每个会话 → 侧栏一行要用的东西（标题、预览、未读、置顶、免打扰）。

        会话列表以前长在这个控件里；现在它长在侧栏的设备列表上（规格见
        ``docs/UI_LAYOUT.md``），所以这里只负责把"每一行长什么样"算出来。
        """
        rows: list[dict[str, Any]] = []
        for conversation in self.conversations():
            conv_id = str(conversation.get("id") or "")
            if not conv_id:
                continue
            rows.append(
                {
                    "id": conv_id,
                    "title": self._conversation_title(conversation),
                    "preview": self._conversation_preview(conversation),
                    "when": self._conversation_time(conversation),
                    "unread": int(conversation.get("unread") or 0),
                    "pinned": self._prefs_pinned(conversation),
                    "muted": self._prefs_muted(conv_id),
                    "is_group": conversation.get("kind") == "group" or conv_id.startswith("g:"),
                    "people": self._conversation_people(conversation),
                    "members": [str(m) for m in (conversation.get("members") or []) if str(m)],
                    "updated": float(conversation.get("updated") or 0),
                }
            )
        # 置顶的排前面，其余按最后活动时间（聊天软件都是这么排的）。
        rows.sort(key=lambda row: (0 if row["pinned"] else 1, -float(row["updated"] or 0)))
        return rows

    def reload(self) -> None:
        """重画当前会话；会话列表有变化时通知侧栏（没有变化就不打扰它）。"""
        rows = self.conversation_rows()
        signature = tuple(
            (row["id"], row["title"], row["preview"], row["unread"], row["pinned"], row["muted"])
            for row in rows
        )
        if signature != self._conversation_sig:
            self._conversation_sig = signature
            self.conversations_changed.emit(rows)
        if not self._conversation and rows:
            # 什么都没打开时自动打开最近那个会话 —— 以前进来就选中列表第一条，
            # 现在列表是侧栏的设备行，这条规则不能丢。
            # **但不标已读**：窗口可能只是在后台开着，用户根本没看（审计员实测
            # 真实数据 SUM(unread) 15→14，用户没点任何东西；而且未读是三端共用
            # 的 chat.db，手机上的未读也会跟着消失）。用户点进去才算看过。
            self.open_conversation(str(rows[0]["id"]), mark_read=False)
            return
        self._reload_messages()

    # -- 打开哪个会话 ------------------------------------------------------

    def current_conversation(self) -> str:
        return self._conversation

    def open_conversation(self, conv_id: str, notify: bool = True, mark_read: bool = True) -> None:
        """打开一个会话（点侧栏里的设备或群聊走的就是这里）。

        ``mark_read=False`` 用在"程序自己恢复上次的会话"这种路径上：那是替用户
        打开，不是用户在看 —— 一启动就把未读清掉，用户会以为自己漏看了消息。
        """
        self._conversation = str(conv_id or "")
        self._rendered = []
        if self._conversation and mark_read:
            try:
                self.engine.chat.mark_read(self._conversation)
            except Exception:
                pass
        # PIN 输入框只在对方要求 PIN 时出现：换了一段会话就重新判断一次。
        self._pin_required = False
        self._refresh_pin_field()
        self._reload_messages()
        if notify:
            self.conversation_changed.emit(self._conversation)

    def conversation_for_member(self, member_id: str, title: str = "") -> str:
        """和某台设备的会话 id；还没有就建一个（点设备 = 开始会话）。"""
        mine = self.engine.info.device_id
        conv_id = direct_conversation_id(mine, member_id)
        if not self.engine.chat.conversation(conv_id):
            self.engine.chat.upsert_conversation(
                conv_id,
                kind="direct",
                title=title or f"和 {self._member_label(member_id)} 的对话",
                members=[mine, member_id],
            )
        return conv_id

    # -- 会话偏好：置顶 / 免打扰（像聊天软件那样右键设置） ------------------

    @property
    def _prefs_path(self) -> str:
        return os.path.join(self.engine.config.data_dir, "chat_ui.json")

    def _prefs(self) -> dict[str, list[str]]:
        if not hasattr(self, "_prefs_cache"):
            try:
                with open(self._prefs_path, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
                self._prefs_cache = {
                    "pinned": [str(x) for x in data.get("pinned", [])],
                    "muted": [str(x) for x in data.get("muted", [])],
                }
            except (OSError, ValueError):
                self._prefs_cache = {"pinned": [], "muted": []}
        return self._prefs_cache

    def _save_prefs(self) -> None:
        try:
            with open(self._prefs_path, "w", encoding="utf-8") as fh:
                json.dump(self._prefs(), fh, ensure_ascii=False, indent=2)
        except OSError:
            pass

    def _prefs_pinned(self, conversation: dict[str, Any]) -> bool:
        conv_id = str(conversation.get("id") or "")
        # 手机那边的会话 id 里带着对方地址，所以置顶既可以按 id 记，也可以按人名记。
        return conv_id in self._prefs()["pinned"] or (
            "pin:" + self._conversation_title(conversation)
        ) in self._prefs()["pinned"]

    def _prefs_muted(self, conv_id: str) -> bool:
        return conv_id in self._prefs()["muted"]

    def _toggle_pref(self, kind: str, conversation: dict[str, Any]) -> None:
        """置顶/免打扰按会话 id 记（稳），同时记一条 "pin:名字"，这样同名会话
        在重装/换 id 之后也还能认出用户的意图。"""
        conv_id = str(conversation.get("id") or "")
        keys = [conv_id]
        if kind == "pinned":
            keys.append("pin:" + self._conversation_title(conversation))
        bucket = self._prefs()[kind]
        if any(key in bucket for key in keys):
            for key in keys:
                if key in bucket:
                    bucket.remove(key)
        else:
            bucket.extend(keys)
        self._save_prefs()
        self.reload()

    @Slot(QPoint)
    def conversation(self, conv_id: str) -> dict[str, Any] | None:
        """The conversation with this id, or ``None``."""
        return next((c for c in self.conversations() if str(c.get("id")) == str(conv_id)), None)

    def build_conversation_menu(self, conv_id: str, menu: QMenu) -> dict[str, Any]:
        """给侧栏用：往 ``menu`` 里挂「置顶 / 免打扰 / 标记已读」。

        会话列表现在长在侧栏里，所以菜单由窗口弹出，但"置顶/免打扰记在哪、
        怎么算一个会话"只有这一份实现（偏好文件在 ``chat_ui.json``）。
        返回 ``{名字: QAction}``，调用方 ``exec`` 之后交回
        :meth:`apply_conversation_menu`。
        """
        conversation = self.conversation(conv_id)
        if conversation is None:
            return {}
        return {
            "pinned": menu.addAction(
                "取消置顶" if self._prefs_pinned(conversation) else "置顶会话"
            ),
            "muted": menu.addAction(
                "取消免打扰" if self._prefs_muted(conv_id) else "消息免打扰"
            ),
            "read": menu.addAction("标记为已读"),
        }

    def apply_conversation_menu(self, chosen, actions: dict[str, Any], conv_id: str) -> None:
        """Run whichever entry the user picked (see :meth:`build_conversation_menu`)."""
        conversation = self.conversation(conv_id)
        if chosen is None or conversation is None:
            return
        if chosen is actions.get("pinned"):
            self._toggle_pref("pinned", conversation)
        elif chosen is actions.get("muted"):
            self._toggle_pref("muted", conversation)
        elif chosen is actions.get("read"):
            try:
                self.engine.chat.mark_read(conv_id)
            except Exception:
                pass
            self.reload()

    def _conversation_preview(self, conversation: dict[str, Any]) -> str:
        last = str(conversation.get("lastText") or "").strip() or "还没有消息"
        if len(last) > 24:
            last = last[:24] + "…"
        return last

    def _conversation_time(self, conversation: dict[str, Any]) -> str:
        """When it last moved, written the way a chat app writes it."""
        stamp = float(conversation.get("updated") or 0)
        if stamp <= 0:
            return ""
        when = time.localtime(stamp)
        today = time.localtime()
        if (when.tm_year, when.tm_yday) == (today.tm_year, today.tm_yday):
            return time.strftime("%H:%M", when)
        if (today.tm_yday - when.tm_yday) == 1 and when.tm_year == today.tm_year:
            return "昨天"
        if abs(time.time() - stamp) < 7 * 86400:
            return "周" + "一二三四五六日"[when.tm_wday]
        return time.strftime("%m-%d", when)

    def _member_labels(self, conversation: dict[str, Any]) -> dict[str, str]:
        """成员 id → 名字（系统 · 版本 · IP）；名字怎么算只有一份实现。"""
        members = [str(m) for m in (conversation.get("members") or []) if str(m)]
        return {member: self._member_label(member) for member in members}

    def _conversation_people(self, conversation: dict[str, Any]) -> str:
        """Who is in this conversation, as names (never raw ids)."""
        return conversation_people(
            conversation, self._member_labels(conversation), self.engine.info.device_id
        )

    def _conversation_title(self, conversation: dict[str, Any]) -> str:
        """A conversation as a human reads it: 对方的名字 / 群名。

        以前列表里直接写会话 id（``d:8765510e214069``）—— 那是数据库主键，
        不是给人看的。规则本身在 ``core.chat.conversation_title`` 里，手机 App
        和网页版显示的是同一个名字（服务端 /api/chat 也用这个函数）。
        """
        return conversation_title(
            conversation, self._member_labels(conversation), self.engine.info.device_id
        )

    # -- rendering ---------------------------------------------------------

    def _reload_messages(self) -> None:
        if not self._conversation:
            self._clear_messages()
            self._canvas_conv = ""
            self._rendered = []
            self._add_empty_state(
                "从左边选一台设备",
                "点设备列表里的任意一台，就能和它聊天、传文件；手机扫码连上后也会出现在那里。",
            )
            return
        conversation = self.engine.chat.conversation(self._conversation) or {}
        messages = self.engine.chat.messages(self._conversation, limit=200)
        # 只有"同一段会话、同一批消息、同一批状态"才跳过重画。换过会话就必须重画
        # （哪怕新那段是空的），否则旧气泡会冒充新会话的内容留在屏幕上。
        #
        # 状态也要参与比较：一条消息从「发送中」变成「待发送（对方不在线）」时
        # id 没变，只看 id 的话气泡永远停在旧状态上 —— 用户看不到时钟角标，
        # 也看不到「立即重试 / 放弃」（C14）。
        signature = [(str(m["id"]), str(m.get("state") or "")) for m in messages]
        if self._canvas_conv == self._conversation and signature == self._rendered:
            return
        self._rendered = signature
        self._clear_messages()
        self._canvas_conv = self._conversation
        if not messages:
            self._add_system(
                "还没有消息。说点什么吧。"
                if conversation
                else "这段会话已经不在了。在左边点一台设备，重新打开一段会话。"
            )
        # 按日期分组：今天 / 昨天 / 更早（宪章 §3.3）。分隔条只在跨天时插一条，
        # 同一天里连着几十条消息不会每三条就冒出一行日期。
        group = ""
        for message in messages:
            bucket = self._date_group(message.get("ts") or 0)
            if bucket != group:
                group = bucket
                self._add_date_separator(bucket)
            self._add_bubble(message)
        self._refresh_queue_banner()
        QTimer.singleShot(0, self._scroll_to_bottom)

    @staticmethod
    def _date_group(stamp: Any) -> str:
        """一条消息属于哪一组：``今天`` / ``昨天`` / ``更早``。"""
        try:
            when = time.localtime(float(stamp or 0))
        except (TypeError, ValueError, OSError):
            return DATE_GROUPS[2]
        now = time.localtime()
        if (when.tm_year, when.tm_yday) == (now.tm_year, now.tm_yday):
            return DATE_GROUPS[0]
        yesterday = time.localtime(time.time() - 86400)
        if (when.tm_year, when.tm_yday) == (yesterday.tm_year, yesterday.tm_yday):
            return DATE_GROUPS[1]
        return DATE_GROUPS[2]

    def _add_date_separator(self, text: str) -> None:
        """一条日期分隔线：中间一句话，两侧各一条发丝线。"""
        holder = QWidget()
        row = QHBoxLayout(holder)
        row.setContentsMargins(theme.SPACE["lg"], theme.SPACE["xs"], theme.SPACE["lg"], theme.SPACE["xs"])
        row.setSpacing(theme.SPACE["sm"])
        row.addStretch(1)
        label = QLabel(text)
        label.setObjectName("Subtitle")
        label.setAlignment(Qt.AlignCenter)
        row.addWidget(label)
        row.addStretch(1)
        self.messages_layout.insertWidget(self.messages_layout.count() - 1, holder)
        return holder

    def _add_empty_state(self, title: str, hint: str) -> None:
        """会话页的空状态：一句话说清"该干什么"。"""
        box = QWidget()
        layout = QVBoxLayout(box)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(theme.SPACE["xs"])
        layout.addStretch(1)
        headline = QLabel(title)
        headline.setObjectName("EmptyTitle")
        headline.setAlignment(Qt.AlignCenter)
        layout.addWidget(headline)
        detail = QLabel(hint)
        detail.setObjectName("EmptyHint")
        detail.setAlignment(Qt.AlignCenter)
        detail.setWordWrap(True)
        layout.addWidget(detail)
        layout.addStretch(1)
        self.messages_layout.insertWidget(self.messages_layout.count() - 1, box)

    #: 系统代号 -> 中文，聊天里显示给用户看
    PLATFORM_NAMES = {
        "linux": "Linux",
        "windows": "Windows",
        "darwin": "macOS",
        "macos": "macOS",
        "android": "安卓",
        "ios": "iOS",
        "browser": "网页版",
    }

    #: 这些是"客户端类型"而不是"设备名字"。手机上装的是韧传，它自己叫 V2408A；
    #: 消息里如果只存了类型（历史消息就是这么存的），显示时补上真名字。
    GENERIC_SENDER_NAMES = ("安卓 App", "网页版", "手机", "未知客户端", "命令行/脚本", "")

    def _sender_label(self, message: dict[str, Any]) -> str:
        """一条消息署谁的名：优先设备自己的名字，其次才是客户端类型。"""
        stored = str(message.get("senderName") or "").strip()
        if stored and stored not in self.GENERIC_SENDER_NAMES:
            return stored
        member = str(message.get("sender") or "")
        if member:
            resolved = self._member_label(member)
            # _member_label 会把"（系统 · 版本 · IP）"缀在后面，署名只要名字。
            resolved = resolved.split("（")[0].strip()
            if resolved and resolved not in ("未知设备", ""):
                return resolved
        return stored or "对方"

    def _member_label(self, member: str) -> str:
        """A member as the user should read it: 名字（系统 · 韧传 版本 · IP）.

        The raw member id is a device id (``web:<key>`` for a phone), which is
        meaningless in a chat.  Everything needed to describe the device is
        already on hand -- the peer list, or the paired-client list for phones.
        """
        if member.startswith("web:"):
            for client in getattr(self, "_clients_provider", lambda: [])() or []:
                # 成员 id 与注册表键不一定同一个写法（App 是 android:<id>，
                # 消息头里却是 web:<id>），所以按别名表匹配。
                if member in client_aliases(client):
                    is_app = str(client.get("kind") or "") == "app"
                    parts = ["安卓 App" if is_app else "网页版"]
                    if client.get("version"):
                        parts.append(f"韧传 {client['version']}")
                    if client.get("address"):
                        parts.append(str(client["address"]))
                    # 名字优先用设备自称的那个（V2408A），label 只是"手机"这类
                    # 类型说明 —— 会话里显示的应该是设备名。
                    shown = str(client.get("name") or client.get("label") or "手机")
                    return f"{shown}（{' · '.join(parts)}）"
            return "手机（网页版）"
        for peer in self.engine.devices():
            if peer.info.device_id == member:
                return f"{peer.info.name}（{self._peer_facts(peer)}）"
        if member == self.engine.info.device_id:
            return f"{self.engine.info.name}（本机）"
        return member[:10]

    def _peer_facts(self, peer) -> str:
        """系统 · 韧传 版本 · IP —— 聊天和设备卡片用同一套说法。"""
        info = peer.info
        parts = [self.PLATFORM_NAMES.get(str(info.platform).lower(), str(info.platform or "未知系统"))]
        if info.version:
            parts.append(f"韧传 {info.version}")
        if peer.address:
            parts.append(str(peer.address))
        return " · ".join(parts)

    def _on_tick(self) -> None:
        """每 1.5 秒：重画列表（未读/置顶会变），刷新气泡进度与待发队列。"""
        self.reload()
        self._refresh_media_progress()
        self._refresh_pin_field()
        self._refresh_queue_banner()

    def set_transfer_progress(
        self, name: str, percent: float | None, text: str, state_text: str | None = None
    ) -> bool:
        """把进度写进会话里对应的那条记录，返回是否找到。

        「发给手机」这条路和局域网直传不一样：手机是 HTTP 客户端，电脑**推不过去**，
        只能把文件交给它来取。这时 ``engine.active_transfers()`` 里什么都没有，
        `_refresh_media_progress` 自然刷不到进度 —— 但用户在会话里看到的那条记录
        一样需要进度，否则就是"文件发出去了，然后什么都不知道"。

        进度由网页服务端在**发字节的时候**报上来（``note_share_progress``），
        这里只负责把它写到气泡上：进度条 + 「42% · 120 MB / 572 MB」。

        ``state_text`` 顺手改掉气泡上那行状态说明 —— 手机那条路上写「发送中…」
        是假话：这一步只是"把文件交给手机页面"，能不能取到要看手机。
        """
        wanted = os.path.basename(str(name or ""))
        if not wanted:
            return False
        candidates: list[tuple[QWidget, dict[str, Any]]] = []
        for bubble, message in list(self._media_bubbles):
            if getattr(bubble, "_progress_label", None) is None:
                continue
            if os.path.basename(str(message.get("mediaName") or "")) != wanted:
                continue
            candidates.append((bubble, message))
        if not candidates:
            return False
        # 同一个文件名既可能躺在"我发出去的"气泡里，也可能躺在"对方发来的"里 ——
        # 进度说的是我发出去的那一条，先认方向，别再写错气泡。
        outgoing = [pair for pair in candidates if pair[1].get("direction") == "out"]
        candidates = outgoing or candidates
        for bubble, _message in candidates:
            self._paint_bubble_progress(bubble, percent, text)
            if state_text is not None:
                note = getattr(bubble, "_state_note", None)
                if note is not None and note.text() != state_text:
                    note.setText(state_text)
            bubble.setToolTip(
                f"{wanted} · {percent:.0f}%" if (text and percent is not None) else wanted
            )
            bubble._share_managed = bool(text)  # noqa: SLF001 - our own widget
            return True
        return False

    @staticmethod
    def _paint_bubble_progress(bubble: QWidget, percent: float | None, text: str) -> None:
        """把"进度 + 一句话"画到气泡上：进度条和百分比一起给（§2.4.3）。

        ``percent=None`` = 大小还不知道（刚交给手机）→ 不确定态的条，
        不是 0% 卡住。用的控件由 desktop-chat 提供（``TransferProgress``，
        文字 + 细进度条画在一起）；拿不到时退回一个 QLabel + QProgressBar。
        """
        label = getattr(bubble, "_progress_label", None)
        if label is None:
            return
        painter = getattr(label, "set_progress", None)
        if callable(painter):
            if str(label.text()) != text or getattr(label, "percent", lambda: None)() != percent:
                painter(percent, text)
        else:
            if label.text() != text:
                label.setText(text)
            bar = getattr(bubble, "_progress_bar", None)
            if bar is not None:
                if percent is None:
                    bar.setRange(0, 0)
                else:
                    bar.setRange(0, 1000)
                    bar.setValue(int(max(0.0, min(100.0, float(percent))) * 10))
                bar.setVisible(bool(text))
        label.setVisible(bool(text))

    def _refresh_media_progress(self) -> None:
        """把活动传输的进度写进对应的附件气泡。

        「传输」页和聊天页看的是同一份事实（``engine.active_transfers()``），
        所以在聊天里发文件时，进度就长在那条消息下面 —— 不用切换页签去猜。
        """
        if not self._media_bubbles:
            return
        try:
            active = self.engine.active_transfers()
        except Exception:
            return
        by_name: dict[str, Any] = {}
        for transfer in active:
            for item in transfer.items.values():
                name = os.path.basename(getattr(item.entry, "name", "") or item.path or "")
                if name:
                    by_name[name] = (transfer, item)
        if os.environ.get("EVERSEND_DEBUG_PROGRESS"):
            # 排查「会话里看不到进度」用：气泡名单与活动传输名单一起打出来。
            print(
                f"[progress] bubbles={[str(m.get('mediaName') or '') for _b, m in self._media_bubbles]} "
                f"active={sorted(by_name)}",
                file=sys.stderr,
                flush=True,
            )
        for bubble, message in list(self._media_bubbles):
            label = getattr(bubble, "_progress_label", None)
            retry = getattr(bubble, "_retry_button", None)
            if label is None:
                continue
            name = str(message.get("mediaName") or "")
            found = by_name.get(name)
            if found is None:
                # 只有"这条气泡的进度本来是这里画的"才由这里收起来。手机那条路
                # （窗口写的"手机正在取 42% / ✅ 手机已取走"）在这里没有活动传输，
                # 以前会被这一行悄悄抹掉 —— 手机在下载，会话里却什么都没有。
                if getattr(bubble, "_lan_progress_shown", False):
                    self._paint_bubble_progress(bubble, 0.0, "")
                    bubble._lan_progress_shown = False  # noqa: SLF001 - our own widget
                if retry is not None and not getattr(bubble, "_share_managed", False):
                    retry.setVisible(
                        message.get("direction") == "out" and message.get("state") == "failed"
                    )
                continue
            transfer, item = found
            total = max(1, int(item.entry.size or 1))
            done = int(getattr(item, "done_bytes", 0) or 0)
            percent = min(100.0, 100.0 * done / total)
            speed = ""
            try:
                speed = human_speed(transfer.stats.instant_speed_bps)
            except Exception:
                speed = ""
            self._paint_bubble_progress(
                bubble,
                percent,
                f"⬆ 传输中 {percent:.0f}%"
                + (f" · {speed}" if speed else "")
                + f" · {human_bytes(done)} / {human_bytes(total)}",
            )
            bubble._lan_progress_shown = True  # noqa: SLF001 - our own widget
            if retry is not None:
                retry.setVisible(False)

    def _retry_attachment(self, message: dict[str, Any]) -> None:
        """重发一条发送失败的附件（就地重试，不用重新选文件）。"""
        source = str(message.get("mediaSource") or "")
        if not source or not os.path.isfile(source):
            QMessageBox.information(self, "找不到原文件", "这个文件本机已经没有了，请重新选择。")
            return
        self.send_attachment(source)

    def _clear_messages(self) -> None:
        self._media_bubbles = []
        while self.messages_layout.count() > 1:
            item = self.messages_layout.takeAt(0)
            widget = item.widget()
            if widget is None:
                continue
            # deleteLater() 只是把删除排进队列：在事件循环真的处理它之前，
            # 控件依然是 messages_host 的子控件，依然按上一次的几何画在原地 ——
            # 换会话时用户看到的就是"上一段对话的气泡还留在画布上"，而且因为
            # 已经脱离布局，它们会被拉成一个个空框。所以先摘干净再排队删除。
            widget.hide()
            widget.setParent(None)
            widget.deleteLater()

    def _add_system(self, text: str) -> None:
        label = QLabel(text)
        label.setObjectName("Subtitle")
        label.setAlignment(Qt.AlignCenter)
        self.messages_layout.insertWidget(self.messages_layout.count() - 1, label)

    def _add_bubble(self, message: dict[str, Any]) -> None:
        outgoing = message.get("direction") == "out"
        bubble = MessageBubble(outgoing)
        layout = QVBoxLayout(bubble)
        layout.setContentsMargins(
            theme.SPACE["md"], theme.SPACE["sm"], theme.SPACE["md"], theme.SPACE["sm"]
        )
        layout.setSpacing(theme.SPACE["xs"])

        who = self._sender_label(message)
        meta = QLabel(f"{who} · {time.strftime('%H:%M', time.localtime(message.get('ts') or 0))}")
        meta.setObjectName("BubbleMeta")
        layout.addWidget(meta)

        kind = message.get("kind")
        if kind == "text" or kind == "system":
            body = emoji_label(message.get("text") or "")
            body.setTextInteractionFlags(Qt.TextSelectableByMouse)
            layout.addWidget(body)
        elif kind in MEDIA_KINDS:
            layout.addWidget(self._attachment_widget(message))
            if message.get("text"):
                layout.addWidget(emoji_label(str(message.get("text") or "")))
        else:
            layout.addWidget(emoji_label(str(message.get("text") or "")))

        state = str(message.get("state") or "")
        queued = outgoing and state in QUEUED_STATES
        state_retry: QPushButton | None = None
        state_note: QLabel | None = None
        if outgoing and state == "sending":
            note = QLabel("发送中…")
            note.setObjectName("BubbleState")
            layout.addWidget(note)
            state_note = note
        elif queued:
            # 对方不在线：气泡右下角一个时钟角标 + 悬停「等待对方上线」，
            # 下面给出「立即重试 / 放弃」（宪章 §3.3 / §5.5）。
            badge = QLabel()
            badge.setPixmap(badge_pixmap("clock", theme.WARNING, 13))
            badge.setFixedSize(13, 13)
            badge.setToolTip(self._queued_tooltip(message))
            bubble.set_corner_badge(badge)
            note = QLabel(self._queued_text(message))
            note.setObjectName("BubbleState")
            layout.addWidget(note)
            state_retry = self._add_state_actions(layout, message, "立即重试")
            bubble._queued_retry = state_retry  # noqa: SLF001 - our own widget
        elif outgoing and state == "failed":
            note = QLabel(self._failed_text(message))
            note.setObjectName("BubbleState")
            layout.addWidget(note)
            state_retry = self._add_state_actions(layout, message, "重试")
        # 附件：把"文件传输"直接长在气泡里（用户在聊天里发文件，就不该再去
        # 「传输」页找进度）。有活动传输时显示百分比与速度，失败给「重试」。
        if kind in MEDIA_KINDS:
            # 进度条 + 百分比文字。以前只有一个文字标签，而"手机正在取"这条
            # 进度由窗口每 2 秒写一次、又被这里每 1.5 秒的刷新抹掉 —— 用户看到
            # 的是"文件发出去了，然后什么都不知道"（docs/REAL_DEVICE_FINDINGS §2）。
            bar = None
            if TransferProgress is not None:
                progress = TransferProgress()
            else:
                progress = QLabel("")
                progress.setObjectName("BubbleState")
                bar = QProgressBar()
                bar.setObjectName("BubbleProgress")
                bar.setRange(0, 1000)
                bar.setValue(0)
                bar.setTextVisible(False)
                bar.setFixedHeight(8)
                bar.setVisible(False)
            progress.setVisible(False)
            layout.addWidget(progress)
            if bar is not None:
                layout.addWidget(bar)
            bubble._progress_bar = bar  # noqa: SLF001 - our own widget
            bubble._progress_label = progress  # noqa: SLF001 - our own widget
            bubble._state_note = state_note  # noqa: SLF001 - our own widget
            #: 这条气泡的进度是不是"电脑发布、手机来取"那条路写的。窗口
            #: （``MainWindow._refresh_phone_shares``）写进去之后，
            #: ``_refresh_media_progress`` 不许再按"没有活动传输"把它抹掉。
            bubble._share_managed = False  # noqa: SLF001 - our own widget
            bubble._lan_progress_shown = False  # noqa: SLF001 - our own widget
            if state_retry is not None:
                # 失败/排队时上面那一排已经有「重试」了，复用它 —— 一个动作一个按钮。
                bubble._retry_button = state_retry  # noqa: SLF001
            else:
                retry = QPushButton("重试")
                retry.setObjectName("CellAction")
                retry.setToolTip("重新投递这个文件")
                retry.setVisible(False)
                retry.clicked.connect(
                    lambda _=False, m=dict(message): self._retry_message(m)
                )
                layout.addWidget(retry)
                bubble._retry_button = retry  # noqa: SLF001
            self._media_bubbles.append((bubble, message))

        row = QHBoxLayout()
        row.addStretch(1 if outgoing else 0)
        row.addWidget(bubble)
        row.addStretch(0 if outgoing else 1)
        holder = QWidget()
        holder.setLayout(row)
        self.messages_layout.insertWidget(self.messages_layout.count() - 1, holder)

    def _add_state_actions(
        self, layout: QVBoxLayout, message: dict[str, Any], retry_label: str
    ) -> QPushButton:
        """排队/失败时那一排按钮：「重试」+「放弃」（宪章 §5.5：可重试、可放弃）。

        返回重试按钮 —— 附件气泡会把它复用成进度逻辑里的 ``_retry_button``，
        免得同一条消息上出现两个"重试"。
        """
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(theme.SPACE["xs"])
        retry = QPushButton(retry_label)
        retry.setObjectName("CellAction")
        retry.setToolTip(
            "不等对方上线，马上再投一次" if retry_label == "立即重试" else "重新投递这条消息"
        )
        retry.clicked.connect(lambda _=False, m=dict(message): self._retry_message(m))
        row.addWidget(retry)
        give_up = QPushButton("放弃")
        give_up.setObjectName("CellAction")
        give_up.setToolTip("不再重试这条消息（对方上线后也不会补发）")
        give_up.clicked.connect(lambda _=False, m=dict(message): self._drop_message(m))
        row.addWidget(give_up)
        row.addStretch(1)
        layout.addLayout(row)
        return retry

    # -- 待发送队列：气泡上的状态文案（能问核心就问，问不到给通用说法） -------

    def _message_state_info(self, message: dict[str, Any]) -> dict[str, Any]:
        lookup = getattr(self.engine, "message_state", None)
        message_id = str(message.get("id") or "")
        if lookup is None or not message_id:
            return {}
        try:
            return dict(lookup(message_id) or {})
        except Exception:
            return {}

    def _queued_text(self, message: dict[str, Any]) -> str:
        info = self._message_state_info(message)
        reason = str(info.get("reason") or "").strip()
        attempts = int(info.get("attempts") or 0)
        base = reason or "已进入待发队列，等对方上线后自动补投"
        # 核心给的 reason 里通常已经写了「第 2/5 次尝试」，那就别再重复一遍。
        if attempts and "次" not in base:
            base += f"（已尝试 {attempts} 次）"
        return base

    def _queued_tooltip(self, message: dict[str, Any]) -> str:
        info = self._message_state_info(message)
        lines = ["等待对方上线：对面一连上就自动发出去。"]
        reason = str(info.get("reason") or "").strip()
        if reason:
            lines.append(reason)
        next_retry = info.get("nextRetryIn", info.get("next_retry_in"))
        try:
            seconds = float(next_retry or 0)
        except (TypeError, ValueError):
            seconds = 0.0
        if seconds > 0:
            lines.append(f"下次自动重试：{seconds:.0f} 秒后")
        lines.append("也可以点「立即重试」或「放弃」。")
        return "\n".join(lines)

    def _failed_text(self, message: dict[str, Any]) -> str:
        """失败那条消息下面写什么 —— 一定是**真实原因**，不是通用安慰话。

        「发给手机」这条路没有聊天投递队列（``engine.message_state`` 里查不到
        reason），原因只有窗口知道（文件读不到、手机掉线、分享被清掉……），所以
        窗口通过 ``set_failure_reason_provider`` 把原因交进来。没有原因时最后
        兜底才是那句通用话 —— 有原因还写"重试用尽"就是在骗用户。
        """
        info = self._message_state_info(message)
        reason = str(info.get("reason") or "").strip()
        if not reason:
            provider = getattr(self, "_failure_reason_provider", None)
            if callable(provider) and str(message.get("kind") or "") in MEDIA_KINDS:
                try:
                    reason = str(provider(str(message.get("id") or "")) or "").strip()
                except Exception:
                    reason = ""
        if not reason and str(message.get("kind") or "") in MEDIA_KINDS:
            # 传输记录自己带着原因（窗口把原因写在消息正文里，见
            # ``MainWindow._fail_paths_in_chat``）：气泡上已经写着，就别再补一句
            # "重试用尽，或者从来没发现过这台设备"——那句话这里不是事实。
            if str(message.get("text") or "").strip():
                return "发送失败"
        return reason or "发送失败：重试用尽，或者从来没发现过这台设备。"

    def set_failure_reason_provider(self, provider) -> None:
        """交一个 ``callable(message_id) -> 中文原因`` 给气泡（见 :meth:`_failed_text`）。"""
        self._failure_reason_provider = provider

    def reload_now(self) -> None:
        """强制重画一遍当前会话（状态没变、但气泡上的说明变了时用）。"""
        self._rendered = []
        self._reload_messages()

    def _attach_context_menu(self, widget: QWidget, path: str, name: str) -> None:
        """给文件卡片挂右键菜单：和气泡、传输表历史行完全同一套动作（宪章 §4）。"""
        widget.setContextMenuPolicy(Qt.CustomContextMenu)
        widget.customContextMenuRequested.connect(
            lambda point, w=widget, p=path, n=name: file_actions_menu(
                w, p, n, resend=self._resend_file
            ).exec(w.mapToGlobal(point))
        )

    def _resend_file(self, path: str) -> None:
        """「重新发送」：把这个文件再发一次给当前会话的对方。"""
        if not path or not os.path.isfile(path):
            self.status_message("原文件已经不在这台机器上了，没法重发。")
            return
        self.send_attachment(path)

    def _attachment_widget(self, message: dict[str, Any]) -> QWidget:
        rel = str(message.get("mediaRel") or "")
        path = os.path.join(self.engine.config.receive_dir, rel) if rel else ""
        # A picture *we* sent never lands in our own receive folder, so the
        # bubble used to show a bare file card for it.  The store keeps where
        # this machine's copy lives (local-only, never sent to the peer); use
        # it when the receive-folder copy is not there.
        source = str(message.get("mediaSource") or "")
        if source and os.path.isfile(source):
            path = source
        name = str(message.get("mediaName") or os.path.basename(rel) or "附件")
        kind = message.get("kind")
        size = int(message.get("mediaSize") or 0)
        exists = bool(path) and os.path.isfile(path)

        box = QWidget()
        layout = QVBoxLayout(box)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(theme.SPACE["xs"])

        if kind == "image" and exists:
            pixmap = QPixmap(path)
            if not pixmap.isNull():
                label = QLabel()
                label.setPixmap(
                    pixmap.scaled(320, 320, Qt.KeepAspectRatio, Qt.SmoothTransformation)
                )
                label.setToolTip(f"{name}（点击看原图）")
                label.setCursor(Qt.PointingHandCursor)
                label.setObjectName("BubbleImage")
                label.mousePressEvent = (  # type: ignore[method-assign]
                    lambda _event, p=path, n=name: self._show_image(p, n)
                )
                layout.addWidget(label)
            else:
                layout.addWidget(QLabel(f"🖼 {name}"))
        else:
            icon = {"image": "🖼", "video": "🎬", "voice": "🎤", "file": "📄"}.get(kind, "📎")
            detail = human_bytes(size) if size else ""
            if kind == "voice" and message.get("durationMs"):
                detail = f"{int(message['durationMs']) / 1000:.0f} 秒" + (
                    f" · {detail}" if detail else ""
                )
            # A video gets a real poster frame when FFmpeg is available (the
            # packaged build can carry one -- see tools/fetch_ffmpeg.py); without
            # it the card stays, and the button says what it will do.  Either
            # way the user sees *something* before clicking.
            preview_path = ""
            if kind == "video" and exists:
                preview_path = media.video_thumbnail(
                    path, self.engine.config.data_dir
                ) or ""
                if not preview_path:
                    seconds = media.media_duration(path, self.engine.config.data_dir)
                    if seconds:
                        detail = f"{seconds:.0f} 秒" + (f" · {detail}" if detail else "")
            if kind == "video" and not preview_path:
                headline = f"{icon}  {name}\n{detail} · 双击「打开」用系统播放器播放"
            else:
                headline = f"{icon}  {name}   {detail}"
            if preview_path:
                pixmap = QPixmap(preview_path)
                if not pixmap.isNull():
                    label = QLabel()
                    label.setPixmap(
                        pixmap.scaled(320, 320, Qt.KeepAspectRatio, Qt.SmoothTransformation)
                    )
                    label.setToolTip(f"{name}（点击播放）")
                    label.setCursor(Qt.PointingHandCursor)
                    label.setObjectName("BubbleImage")
                    label.mousePressEvent = (  # type: ignore[method-assign]
                        lambda _event, p=path: self._play_media(p)
                    )
                    layout.addWidget(label)
            layout.addWidget(QLabel(headline))
            if kind == "voice" and exists and media.find_tool("ffplay", self.engine.config.data_dir):
                # 自带 ffplay 时，桌面端也能直接听语音（Essentials 自己没有音频输出）。
                voice_button = QPushButton("▶ 播放语音（内置播放器）")
                voice_button.clicked.connect(lambda _=False, p=path: self._play_media(p))
                layout.addWidget(voice_button)

        box.mouseDoubleClickEvent = (  # type: ignore[method-assign]
            lambda _event, p=path: self._open_path(p) if p else None
        )
        # 右键：完整文件动作矩阵（和待发方块、传输表历史行同一套，宪章 §4）。
        self._attach_context_menu(box, path, name)

        # 主按钮只有一个：「预览」。其余动作（打开 / 打开所在文件夹 / 另存为…
        # / 复制路径 / 复制文件 / 删除本地文件 / 重新发送 / 属性）都收进 ⋯ 菜单
        # —— 以前这里并排四个平级按钮，用户说"操作逻辑繁复无意义"（宪章 §3.1）。
        buttons = QHBoxLayout()
        buttons.setContentsMargins(0, 0, 0, 0)
        buttons.setSpacing(theme.SPACE["xs"])
        preview_button = QPushButton("预览")
        preview_button.setObjectName("CellAction")
        preview_button.setEnabled(exists)
        preview_button.setToolTip("在这里看一眼内容，不用先打开别的程序")
        preview_button.clicked.connect(
            lambda _=False, p=path, n=name: self.preview_requested.emit(p) if p else None
        )
        buttons.addWidget(preview_button)

        overflow = overflow_menu_button(box, path, name, resend=self._resend_file)
        buttons.addWidget(overflow)
        buttons.addStretch(1)
        layout.addLayout(buttons)
        if not exists:
            missing = QLabel("文件不在这台机器上（可能是对方发来的，尚未落地）")
            missing.setObjectName("BubbleState")
            missing.setWordWrap(True)
            layout.addWidget(missing)
        return box

    def _save_as(self, path: str, name: str) -> None:
        from .widgets import save_as

        target = save_as(self, path, name)
        if target:
            self.status_message(f"已另存为 {target}")

    def _play_media(self, path: str) -> None:
        """Play with the bundled ffplay, or hand the file to the system.

        ``ffplay`` ships with FFmpeg, and when the package carries one (see
        ``tools/fetch_ffmpeg.py``) that is the whole reason for it: PySide6
        Essentials has no audio output and no video surface, so without this the
        only option was "open it in another program".
        """
        if not path or not os.path.isfile(path):
            return
        command = media.play_command(path, self.engine.config.data_dir)
        if command is None:
            self._open_path(path)
            return
        try:
            creation = 0
            if os.name == "nt":  # pragma: no cover - Windows only
                creation = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            subprocess.Popen(command, creationflags=creation)
        except OSError as exc:
            QMessageBox.warning(self, "打不开这个文件", str(exc))

    def _show_image(self, path: str, name: str = "") -> None:
        """Show an attachment full size.

        The bubble shows a 320 px thumbnail so a chat stays readable; a photo
        you cannot actually look at is not a preview, so clicking it opens the
        full-resolution image with a way to save it.

        用的是**同一个**预览窗口（预览矩阵见宪章 §6）：气泡上的「预览」、待发
        方块、传输表历史行看到的东西必须一样。
        """
        if not path or not os.path.isfile(path):
            return
        dialog = FilePreviewDialog(
            path, name or os.path.basename(path), self, self.engine.config.data_dir
        )
        dialog.exec()

    def _open_path(self, path: str) -> None:
        if not path:
            return
        try:
            ok, message = platform_open.open_path_report(path)
        except Exception as exc:  # pragma: no cover - reported to the user
            self.status_message(f"打不开：{exc}")
            return
        if not ok:
            self.status_message(message or "打不开这个文件")

    def _scroll_to_bottom(self) -> None:
        """滚到最新一条。

        气泡的最终高度要等布局跑完才知道：只调一次的话，滚动条的最大值还是旧的，
        用户打开一段长会话就停在中间。所以下一个事件循环与 60ms 各补一次。
        """
        bar = self.scroll.verticalScrollBar()

        def pin() -> None:
            try:
                bar.setValue(bar.maximum())
            except RuntimeError:  # pragma: no cover - 窗口已经关掉了
                pass

        pin()
        QTimer.singleShot(0, pin)
        QTimer.singleShot(60, pin)

    # -- actions -----------------------------------------------------------

    def _toggle_emoji_pad(self) -> None:
        visible = self.emoji_pad.toggle()
        self.emoji_button.setChecked(visible)

    def _pick_emoji(self) -> None:
        """旧名字：直接把面板展开（不再是弹窗）。"""
        self.emoji_pad.setVisible(True)
        self.emoji_button.setChecked(True)

    def _insert_emoji(self, emoji: str) -> None:
        self.input.insert(emoji)
        self.input.setFocus()

    def _attach(self) -> None:
        """📎 传输文件 —— 选文件的事交给窗口（它管着待发列表与设备选择）。"""
        self.attach_requested.emit()

    def new_chat_with(self, member_id: str, name: str = "") -> None:
        """和一台设备开始聊天（点侧栏的设备走的就是这里）。"""
        conv_id = self.conversation_for_member(member_id, f"和 {name} 的对话" if name else "")
        self.reload()
        self.open_conversation(conv_id)

    def new_group(self) -> None:
        members = self._selectable_members()
        if not members:
            QMessageBox.information(self, "还没有可以拉进群的设备", "先让另一台设备连上。")
            return
        dialog = QDialog(self)
        dialog.setWindowTitle("新建群聊")
        layout = QVBoxLayout(dialog)
        layout.addWidget(QLabel("群名称"))
        name_edit = QLineEdit(f"群聊 {time.strftime('%m-%d %H:%M')}")
        layout.addWidget(name_edit)
        layout.addWidget(QLabel("选择成员"))
        picker = QListWidget()
        picker.setSelectionMode(QListWidget.MultiSelection)
        for member_id, name, kind in members:
            item = QListWidgetItem(f"{name}（{kind}）")
            item.setData(Qt.UserRole, member_id)
            picker.addItem(item)
            item.setSelected(True)
        layout.addWidget(picker, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        if dialog.exec() != QDialog.Accepted:
            return
        chosen = [
            picker.item(i).data(Qt.UserRole)
            for i in range(picker.count())
            if picker.item(i).isSelected()
        ]
        if not chosen:
            return
        from ..core.chat import new_group_id

        conv_id = new_group_id()
        title = name_edit.text().strip() or "群聊"
        self.engine.chat.upsert_conversation(
            conv_id, kind="group", title=title, members=[self.engine.info.device_id, *chosen]
        )
        # Announce the group with its first message so every member learns it
        # exists even if it was offline right now.
        self._send(
            conv_id,
            "system",
            f"{self.engine.info.name} 创建了群聊「{title}」",
            members=[self.engine.info.device_id, *chosen],
            title=title,
        )
        self.reload()
        self.open_conversation(conv_id)

    def _selectable_members(self) -> list[tuple[str, str, str]]:
        found: list[tuple[str, str, str]] = []
        for peer in self.engine.devices():
            found.append((peer.info.device_id, peer.info.name, "电脑"))
        for client in getattr(self, "_clients_provider", lambda: [])() or []:
            from ..web.server import is_mobile_client

            if is_mobile_client(client):
                found.append(
                    ("web:" + str(client.get("key") or ""), client.get("label") or "手机", "手机")
                )
        return found

    def send_text(self, text: str) -> None:
        """Send one line of text to the open conversation."""
        text = str(text or "").strip()
        if not text or not self._conversation:
            return
        self._send(self._conversation, "text", text)

    def send_items(self, text: str = "", paths: Iterable[str] = ()) -> None:
        """一句话 + 若干文件，一次发出去（「发送」按钮走的就是这里）。

        一个批次一个后台线程：以前每选一个文件就串行发一次，第二条起会被
        ``_busy`` 挡掉，用户看到的是"只发出去了第一个文件"。
        """
        items: list[tuple[str, str, str]] = []
        body = str(text or "").strip()
        if body:
            items.append(("text", body, ""))
        for path in paths:
            items.append((guess_kind(str(path)), "", str(path)))
        if not items:
            return
        if not self._conversation:
            QMessageBox.information(
                self, "还没有选择设备", "先在左边点一台设备，再发送。"
            )
            return
        self._send_batch(self._conversation, items)

    def conversation_summary(self, conv_id: str) -> tuple[str, str]:
        """(标题, 成员) —— 主区顶部显示群聊时用，规则和会话列表一致。"""
        conversation = self.conversation(conv_id)
        if not conversation:
            return "", ""
        return self._conversation_title(conversation), self._conversation_people(conversation)

    def send_attachment(self, path: str) -> None:
        if not self._conversation:
            QMessageBox.information(
                self, "还没有选择设备", "先在左边点一台设备，再发送文件。"
            )
            return
        kind = guess_kind(path)
        self._send(self._conversation, kind, "", media_path=path)

    def _send(
        self,
        conv_id: str,
        kind: str,
        text: str,
        *,
        media_path: str = "",
        members: list[str] | None = None,
        title: str = "",
    ) -> None:
        self._send_batch(conv_id, [(kind, text, media_path)], members=members, title=title)

    def _send_batch(
        self,
        conv_id: str,
        items: list[tuple[str, str, str]],
        *,
        members: list[str] | None = None,
        title: str = "",
    ) -> None:
        """Send several messages in one background pass (see :meth:`send_items`)."""
        if self._busy:
            self.send_finished.emit(False, "上一条还在发送中")
            return
        self._busy = True
        self.send_button.setEnabled(False)

        def run() -> None:
            error = ""
            try:
                conversation = self.engine.chat.conversation(conv_id) or {}
                to = ""
                for member in conversation.get("members", []):
                    if member != self.engine.info.device_id and not str(member).startswith("web:"):
                        to = member
                        break
                for kind, text, media_path in items:
                    self.engine.send_chat(
                        conv_id,
                        kind=kind,
                        text=text,
                        media_path=media_path,
                        title=title or str(conversation.get("title") or ""),
                        members=members,
                        to=to,
                    )
            except Exception as exc:  # pragma: no cover - reported to the UI
                error = str(exc)
            self._busy = False
            self.send_finished.emit(not error, error)

        def done(ok: bool, error: str) -> None:
            # 需要 PIN 的情况核心只会回一句"PIN required"：这时候把 PIN 输入框
            # 放出来，别让用户对着一句英文/一个空框猜（宪章 C10 / §3.1）。
            if not ok and "pin" in str(error).lower():
                self.set_pin_required(True, "对方要求 PIN，但又没接受：请在输入框右边填 PIN 再发一次")
            self._refresh_queue_banner()

        self.send_finished.connect(done)
        threading.Thread(target=run, name="chat-send", daemon=True).start()


def guess_kind(path: str) -> str:
    """Pick the message kind from the file's extension."""
    ext = os.path.splitext(path)[1].lower()
    if ext in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".heic", ".heif"):
        return "image"
    if ext in (".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v", ".3gp"):
        return "video"
    if ext in (".m4a", ".aac", ".opus", ".ogg", ".mp3", ".wav", ".amr", ".weba"):
        return "voice"
    return "file"


__all__ = ["ChatView", "EMOJI", "EMOJI_GROUPS", "EmojiPicker", "ImageViewer", "guess_kind"]
