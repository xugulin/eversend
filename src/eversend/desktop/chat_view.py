"""The chat tab: conversations on the left, messages on the right.

Design notes
------------
* **One place decides what a message looks like.**  Every message -- typed on
  this computer, typed on a phone, or arrived from another computer -- is stored
  in :class:`~eversend.core.chat.ChatStore` first.  The view renders the store,
  so the three transports cannot drift apart in the UI.
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

import json
import os
import subprocess
import sys
import threading
import time
from typing import Any

from PySide6.QtCore import QPoint, Qt, QTimer, Signal, Slot
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from ..core import media, platform_open
from ..core.chat import MEDIA_KINDS, direct_conversation_id
from ..core.emoji import EMOJI_GROUPS
from ..core.engine import Engine
from ..core.model import human_bytes, human_speed

#: The palette itself lives in the core so all three clients offer the same
#: set (see :mod:`eversend.core.emoji`); this module only draws it.
EMOJI = [emoji for _title, group in EMOJI_GROUPS for emoji in group]


class EmojiPicker(QDialog):
    """A scrollable, grouped emoji palette.

    The old picker was a QMenu with 48 faces and no scrolling, which is why the
    user asked for "more emoji, and let me scroll".  This one shows the whole
    palette grouped by category inside a scroll area; clicking an emoji inserts
    it and keeps the dialog open, so a message can be decorated with several.
    """

    def __init__(self, on_pick, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("选择表情")
        self.resize(420, 520)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)

        area = QScrollArea()
        area.setWidgetResizable(True)
        body = QWidget()
        grid = QVBoxLayout(body)
        grid.setContentsMargins(4, 4, 4, 4)
        grid.setSpacing(10)
        per_row = 8
        for title, group in EMOJI_GROUPS:
            header = QLabel(f"{title}（{len(group)}）")
            header.setObjectName("Subtitle")
            grid.addWidget(header)
            for start in range(0, len(group), per_row):
                row = QHBoxLayout()
                row.setSpacing(2)
                for emoji in group[start : start + per_row]:
                    button = QPushButton(emoji)
                    button.setFixedSize(38, 34)
                    button.setFlat(True)
                    button.setToolTip(emoji)
                    button.clicked.connect(lambda _=False, e=emoji: on_pick(e))
                    row.addWidget(button)
                row.addStretch(1)
                grid.addLayout(row)
        grid.addStretch(1)
        area.setWidget(body)
        layout.addWidget(area, 1)

        buttons = QDialogButtonBox()
        close = buttons.addButton("关闭", QDialogButtonBox.RejectRole)
        close.clicked.connect(self.reject)
        layout.addWidget(buttons)


class ConversationRow(QWidget):
    """One conversation, drawn like a chat app draws them.

    Layout: avatar on the left, then two lines — name (+ time on the right) and
    preview (+ unread badge).  The old list was plain text ("d:8765…｜一对一"),
    which told the user nothing; this is the shape everyone already knows.
    """

    def __init__(
        self,
        *,
        icon: str,
        title: str,
        preview: str,
        when: str,
        unread: int,
        muted: bool = False,
        pinned: bool = False,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("ConvRow")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 6, 8, 6)
        layout.setSpacing(10)

        avatar = QLabel(icon)
        avatar.setObjectName("ConvAvatar")
        avatar.setFixedSize(38, 38)
        avatar.setAlignment(Qt.AlignCenter)
        self.avatar = avatar
        layout.addWidget(avatar)

        body = QVBoxLayout()
        body.setSpacing(2)
        top = QHBoxLayout()
        top.setSpacing(6)
        self.name = QLabel(("📌 " if pinned else "") + title)
        self.name.setObjectName("ConvName")
        top.addWidget(self.name, 1)
        self.when = QLabel(when)
        self.when.setObjectName("ConvTime")
        top.addWidget(self.when)
        body.addLayout(top)

        bottom = QHBoxLayout()
        bottom.setSpacing(6)
        self.preview = QLabel(("🔕 " if muted else "") + preview)
        self.preview.setObjectName("ConvPreview")
        bottom.addWidget(self.preview, 1)
        self.badge = QLabel(f"{unread}" if unread else "")
        self.badge.setObjectName("ConvBadge")
        self.badge.setVisible(bool(unread))
        self.badge.setAlignment(Qt.AlignCenter)
        bottom.addWidget(self.badge)
        body.addLayout(bottom)
        layout.addLayout(body, 1)

    def set_selected(self, selected: bool) -> None:
        """QListWidget paints behind item widgets, so do it here."""
        self.setStyleSheet(
            "QWidget#ConvRow { background: " + ("#2f81f7" if selected else "transparent") + "; border-radius: 8px; }"
            "QLabel { background: transparent; }"
        )


class ChatView(QWidget):
    """The whole 聊天 tab."""

    #: Emitted when a send finished (ok, error) so the window can show it.
    send_finished = Signal(bool, str)

    def __init__(self, engine: Engine, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.engine = engine
        self._conversation = ""
        self._rendered: list[str] = []
        self._busy = False
        #: (气泡, 消息) —— 每次刷新时把传输进度写进气泡里
        self._media_bubbles: list[tuple[QWidget, dict[str, Any]]] = []
        self._build()
        self._tick = QTimer(self)
        self._tick.setInterval(1500)
        self._tick.timeout.connect(self._on_tick)
        self._tick.start()
        self.reload()

    # -- construction ------------------------------------------------------

    def _build(self) -> None:
        root = QHBoxLayout(self)
        root.setContentsMargins(14, 14, 14, 14)
        root.setSpacing(12)

        # ---- left: conversations
        left = QVBoxLayout()
        left.setSpacing(8)
        title = QLabel("会话")
        title.setObjectName("Title")
        left.addWidget(title)
        self.conv_list = QListWidget()
        self.conv_list.setObjectName("ConvList")
        self.conv_list.setWordWrap(True)
        self.conv_list.currentRowChanged.connect(self._on_conversation_changed)
        self.conv_list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.conv_list.customContextMenuRequested.connect(self._conversation_menu)
        left.addWidget(self.conv_list, 1)

        buttons = QHBoxLayout()
        self.new_chat_button = QPushButton("发起聊天")
        self.new_chat_button.setToolTip("选择一台电脑或一台已连接的手机开始聊天")
        self.new_chat_button.clicked.connect(self._new_chat)
        buttons.addWidget(self.new_chat_button)
        self.new_group_button = QPushButton("新建群聊")
        self.new_group_button.clicked.connect(self._new_group)
        buttons.addWidget(self.new_group_button)
        left.addLayout(buttons)

        left_widget = QWidget()
        left_widget.setLayout(left)
        left_widget.setFixedWidth(300)
        root.addWidget(left_widget)

        # ---- right: messages + composer
        right = QVBoxLayout()
        right.setSpacing(8)
        self.header = QLabel("选择一个会话")
        self.header.setObjectName("Title")
        right.addWidget(self.header)
        self.subheader = QLabel("")
        self.subheader.setObjectName("Subtitle")
        self.subheader.setWordWrap(True)
        right.addWidget(self.subheader)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setObjectName("ChatScroll")
        self.messages_host = QWidget()
        self.messages_layout = QVBoxLayout(self.messages_host)
        self.messages_layout.setContentsMargins(6, 6, 6, 6)
        self.messages_layout.setSpacing(8)
        self.messages_layout.addStretch(1)
        self.scroll.setWidget(self.messages_host)
        right.addWidget(self.scroll, 1)

        composer = QHBoxLayout()
        composer.setSpacing(6)
        self.emoji_button = QPushButton("😊")
        self.emoji_button.setFixedWidth(44)
        self.emoji_button.setToolTip("表情")
        self.emoji_button.clicked.connect(self._pick_emoji)
        composer.addWidget(self.emoji_button)

        self.attach_button = QPushButton("📎")
        self.attach_button.setFixedWidth(44)
        self.attach_button.setToolTip("发送文件、图片或视频")
        self.attach_button.clicked.connect(self._attach)
        composer.addWidget(self.attach_button)

        self.input = QLineEdit()
        self.input.setPlaceholderText("输入消息，回车发送")
        self.input.returnPressed.connect(self._send_text)
        composer.addWidget(self.input, 1)

        self.send_button = QPushButton("发送")
        self.send_button.setObjectName("Primary")
        self.send_button.clicked.connect(self._send_text)
        composer.addWidget(self.send_button)
        right.addLayout(composer)

        self.hint = QLabel(
            "文字、表情、图片、视频、文件都在这里收发；手机录的语音会显示成语音条，"
            "点「打开」用系统播放器播放（绿色包精简掉了 Qt 多媒体模块，窗口内不能直接放）。"
        )
        self.hint.setObjectName("Subtitle")
        self.hint.setWordWrap(True)
        right.addWidget(self.hint)

        root.addLayout(right, 1)

    # -- data --------------------------------------------------------------

    def conversations(self) -> list[dict[str, Any]]:
        return self.engine.chat.conversations()

    def reload(self) -> None:
        """Redraw the conversation list and the open conversation."""
        self._reload_conversations()
        self._reload_messages()

    def _reload_conversations(self) -> None:
        conversations = self.conversations()
        # 列表现在是"每行一个控件"（头像 + 名字 + 未读），改文字那条快路径
        # 既刷新不了控件、也不会重排置顶 —— 所以只要用的是控件就整表重建。
        # 会话数量本来就只有几十条，重建的开销可以忽略。
        if self.conv_list.count() and self.conv_list.itemWidget(self.conv_list.item(0)) is not None:
            self._fill_conversations(conversations)
            return
        if self.conv_list.count() != len(conversations):
            self._fill_conversations(conversations)
            return
        for row, conversation in enumerate(conversations):
            item = self.conv_list.item(row)
            if item is None or item.data(Qt.UserRole) != conversation["id"]:
                self._fill_conversations(conversations)
                return
            item.setText(self._conversation_label(conversation))

    def _fill_conversations(self, conversations: list[dict[str, Any]]) -> None:
        self.conv_list.blockSignals(True)
        self.conv_list.clear()
        # 置顶的排前面，其余按最后活动时间（QQ 就是这么排的）。
        ordered = sorted(
            conversations,
            key=lambda c: (
                0 if self._prefs_pinned(c) else 1,
                -float(c.get("updated") or 0),
            ),
        )
        for conversation in ordered:
            conv_id = str(conversation["id"])
            unread = int(conversation.get("unread") or 0)
            is_group = conversation.get("kind") == "group" or conv_id.startswith("g:")
            row = ConversationRow(
                icon="👥" if is_group else "💬",
                title=self._conversation_title(conversation),
                preview=self._conversation_preview(conversation),
                when=self._conversation_time(conversation),
                unread=unread,
                muted=self._prefs_muted(conv_id),
                pinned=self._prefs_pinned(conversation),
            )
            item = QListWidgetItem()
            item.setData(Qt.UserRole, conv_id)
            item.setSizeHint(row.sizeHint())
            item.setToolTip(f"{conv_id}\n{self._conversation_people(conversation)}")
            self.conv_list.addItem(item)
            self.conv_list.setItemWidget(item, row)
        self.conv_list.blockSignals(False)
        self._select_conversation(self._conversation or self._first_id())
        self._paint_selection()

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
    def _conversation_menu(self, position) -> None:
        item = self.conv_list.itemAt(position)
        if item is None:
            return
        conversation = next(
            (c for c in self.conversations() if str(c.get("id")) == str(item.data(Qt.UserRole))),
            None,
        )
        if conversation is None:
            return
        conv_id = str(conversation.get("id") or "")
        menu = QMenu(self)
        pinned = self._prefs_pinned(conversation)
        pinned_action = menu.addAction("取消置顶" if pinned else "置顶会话")
        muted = self._prefs_muted(conv_id)
        muted_action = menu.addAction("取消免打扰" if muted else "消息免打扰")
        menu.addSeparator()
        read_action = menu.addAction("标记为已读")
        chosen = menu.exec(self.conv_list.mapToGlobal(position))
        if chosen is pinned_action:
            self._toggle_pref("pinned", conversation)
        elif chosen is muted_action:
            self._toggle_pref("muted", conversation)
        elif chosen is read_action:
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

    #: 消息种类 -> 会话列表里的前缀，像聊天软件那样一眼看出"最后一条是什么"。
    PREVIEW_ICONS = {
        "image": "[图片]",
        "video": "[视频]",
        "voice": "[语音]",
        "file": "[文件]",
    }

    def _conversation_people(self, conversation: dict[str, Any]) -> str:
        """Who is in this conversation, as names (never raw ids)."""
        members = [str(m) for m in (conversation.get("members") or [])]
        mine = self.engine.info.device_id
        others = [self._member_label(m) for m in members if m != mine]
        return "、".join(others) if others else "只有本机"

    def _conversation_title(self, conversation: dict[str, Any]) -> str:
        """A conversation as a human reads it: 对方的名字 / 群名。

        以前列表里直接写会话 id（``d:8765510e214069``）—— 那是数据库主键，
        不是给人看的。一对一显示对方的设备名，群聊显示群名（没起名就写人数），
        名字实在拿不到（对方从没连过、记录也没了）才退回一句"未知设备"。
        """
        if conversation.get("kind") == "group" or str(conversation.get("id", "")).startswith("g:"):
            title = str(conversation.get("title") or "").strip()
            if title:
                return title
            count = len([m for m in (conversation.get("members") or []) if str(m)]) or 1
            return f"群聊（{count} 人）"
        members = [str(m) for m in (conversation.get("members") or [])]
        mine = self.engine.info.device_id
        others = [m for m in members if m != mine]
        if not others:
            # A 1:1 with a phone whose record is gone, or with ourselves.
            return "（只有自己）" if not members else (self.engine.info.name if not others else "未知设备")
        name = self._member_label(others[0])
        # ``_member_label`` adds the specs in brackets; the list wants the name.
        return name.split("（")[0] or "未知设备"

    def _conversation_label(self, conversation: dict[str, Any]) -> str:
        unread = int(conversation.get("unread") or 0)
        badge = f"  🔴{unread}" if unread else ""
        is_group = conversation.get("kind") == "group" or str(conversation.get("id", "")).startswith("g:")
        icon = "👥" if is_group else "💬"
        title = self._conversation_title(conversation)
        last = str(conversation.get("lastText") or "").strip()
        if not last:
            last = "还没有消息"
        if len(last) > 26:
            last = last[:26] + "…"
        return f"{icon} {title}{badge}\n{last}"

    def _first_id(self) -> str:
        conversations = self.conversations()
        return conversations[0]["id"] if conversations else ""

    def _select_conversation(self, conv_id: str) -> None:
        if not conv_id:
            self._conversation = ""
            return
        for row in range(self.conv_list.count()):
            if self.conv_list.item(row).data(Qt.UserRole) == conv_id:
                self.conv_list.setCurrentRow(row)
                return

    def _paint_selection(self) -> None:
        """给每一行画上"选中/未选中"的背景（item widget 会盖住列表自己的高亮）。"""
        for index in range(self.conv_list.count()):
            item = self.conv_list.item(index)
            widget = self.conv_list.itemWidget(item)
            if widget is not None:
                widget.set_selected(index == self.conv_list.currentRow())

    def _on_conversation_changed(self, row: int) -> None:
        if row < 0:
            return
        item = self.conv_list.item(row)
        if item is None:
            return
        self._conversation = str(item.data(Qt.UserRole) or "")
        self._rendered = []
        self.engine.chat.mark_read(self._conversation)
        self._reload_messages()

    # -- rendering ---------------------------------------------------------

    def _reload_messages(self) -> None:
        if not self._conversation:
            self.header.setText("选择一个会话")
            self.subheader.setText(
                "点「发起聊天」选择一台设备；手机连上后也会出现在列表里。"
            )
            self._clear_messages()
            return
        conversation = self.engine.chat.conversation(self._conversation) or {}
        self.header.setText(conversation.get("title") or self._conversation[:16])
        members = [m for m in conversation.get("members", []) if m]
        mine = self.engine.info.device_id
        others = [self._member_label(m) for m in members if m != mine]
        self.subheader.setText(
            ("群聊 · " if conversation.get("kind") == "group" else "一对一 · ")
            + f"成员：{self.engine.info.name}（本机）"
            + ("、" + "、".join(others) if others else "")
        )
        # 列表和标题栏用同一个名字，避免"列表里叫 A、点进去叫 d:xxx"。
        try:
            self.header.setText(self._conversation_title(conversation))
        except Exception:
            pass
        messages = self.engine.chat.messages(self._conversation, limit=200)
        if [m["id"] for m in messages] == self._rendered:
            return
        self._rendered = [m["id"] for m in messages]
        self._clear_messages()
        if not messages:
            self._add_system("还没有消息。说点什么吧。")
        for message in messages:
            self._add_bubble(message)
        QTimer.singleShot(0, self._scroll_to_bottom)

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

    def _member_label(self, member: str) -> str:
        """A member as the user should read it: 名字（系统 · 韧传 版本 · IP）.

        The raw member id is a device id (``web:<key>`` for a phone), which is
        meaningless in a chat.  Everything needed to describe the device is
        already on hand -- the peer list, or the paired-client list for phones.
        """
        if member.startswith("web:"):
            key = member[4:]
            for client in getattr(self, "_clients_provider", lambda: [])() or []:
                if client.get("key") == key:
                    is_app = str(client.get("kind") or "") == "app"
                    parts = ["安卓 App" if is_app else "网页版"]
                    if client.get("version"):
                        parts.append(f"韧传 {client['version']}")
                    if client.get("address"):
                        parts.append(str(client["address"]))
                    return f"{client.get('label') or '手机'}（{' · '.join(parts)}）"
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
        """每 1.5 秒：重画列表（未读/置顶会变），并刷新气泡里的传输进度。"""
        self.reload()
        self._refresh_media_progress()

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
        for bubble, message in list(self._media_bubbles):
            label = getattr(bubble, "_progress_label", None)
            retry = getattr(bubble, "_retry_button", None)
            if label is None:
                continue
            name = str(message.get("mediaName") or "")
            found = by_name.get(name)
            if found is None:
                label.setVisible(False)
                if retry is not None:
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
            label.setText(
                f"⬆ 传输中 {percent:.0f}%" + (f" · {speed}" if speed else "")
            )
            label.setVisible(True)
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
            if widget is not None:
                widget.deleteLater()

    def _add_system(self, text: str) -> None:
        label = QLabel(text)
        label.setObjectName("Subtitle")
        label.setAlignment(Qt.AlignCenter)
        self.messages_layout.insertWidget(self.messages_layout.count() - 1, label)

    def _add_bubble(self, message: dict[str, Any]) -> None:
        outgoing = message.get("direction") == "out"
        bubble = QFrame()
        bubble.setObjectName("BubbleOut" if outgoing else "BubbleIn")
        bubble.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Preferred)
        layout = QVBoxLayout(bubble)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(4)

        who = message.get("senderName") or self._member_label(str(message.get("sender")))
        meta = QLabel(f"{who} · {time.strftime('%H:%M', time.localtime(message.get('ts') or 0))}")
        meta.setObjectName("BubbleMeta")
        layout.addWidget(meta)

        kind = message.get("kind")
        if kind == "text" or kind == "system":
            body = QLabel(message.get("text") or "")
            body.setWordWrap(True)
            body.setTextInteractionFlags(Qt.TextSelectableByMouse)
            layout.addWidget(body)
        elif kind in MEDIA_KINDS:
            layout.addWidget(self._attachment_widget(message))
            caption = QLabel(str(message.get("text") or ""))
            caption.setWordWrap(True)
            if message.get("text"):
                layout.addWidget(caption)
        else:
            body = QLabel(str(message.get("text") or ""))
            body.setWordWrap(True)
            layout.addWidget(body)

        state = str(message.get("state") or "")
        if outgoing and state in ("sending", "failed"):
            note = QLabel("发送中…" if state == "sending" else "发送失败（对方不在线？）")
            note.setObjectName("BubbleState")
            layout.addWidget(note)
        # 附件：把"文件传输"直接长在气泡里（用户在聊天里发文件，就不该再去
        # 「传输」页找进度）。有活动传输时显示百分比与速度，失败给「重试」。
        if kind in MEDIA_KINDS:
            progress = QLabel("")
            progress.setObjectName("BubbleState")
            progress.setVisible(False)
            layout.addWidget(progress)
            bubble._progress_label = progress  # noqa: SLF001 - our own widget
            retry = QPushButton("重试")
            retry.setVisible(False)
            retry.clicked.connect(lambda _=False, m=dict(message): self._retry_attachment(m))
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
        layout.setSpacing(4)

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
        buttons = QHBoxLayout()
        buttons.setSpacing(6)
        open_button = QPushButton("打开")
        open_button.setEnabled(exists)
        open_button.clicked.connect(lambda _=False, p=path: self._open_path(p))
        buttons.addWidget(open_button)
        folder_button = QPushButton("所在文件夹")
        folder_button.setEnabled(exists)
        folder_button.clicked.connect(lambda _=False, p=path: self._open_path(os.path.dirname(p)))
        buttons.addWidget(folder_button)
        buttons.addStretch(1)
        layout.addLayout(buttons)
        if not exists:
            missing = QLabel("文件不在这台机器上（可能是对方发来的，尚未落地）")
            missing.setObjectName("BubbleState")
            missing.setWordWrap(True)
            layout.addWidget(missing)
        return box

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
        """
        if not path or not os.path.isfile(path):
            return
        dialog = ImageViewer(path, name or os.path.basename(path), self)
        dialog.exec()

    def _open_path(self, path: str) -> None:
        if not path:
            return
        try:
            platform_open.open_path(path)
        except Exception as exc:
            QMessageBox.warning(self, "打不开", f"{path}\n{exc}")

    def _scroll_to_bottom(self) -> None:
        bar = self.scroll.verticalScrollBar()
        bar.setValue(bar.maximum())

    # -- actions -----------------------------------------------------------

    def _pick_emoji(self) -> None:
        dialog = EmojiPicker(self._insert_emoji, self)
        dialog.exec()

    def _insert_emoji(self, emoji: str) -> None:
        self.input.insert(emoji)
        self.input.setFocus()

    def _attach(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(self, "选择要发送的文件")
        if paths:
            self.send_attachment(paths[0])

    def _new_chat(self) -> None:
        members = self._selectable_members()
        if not members:
            QMessageBox.information(
                self,
                "还没有可以聊天的设备",
                "需要先有另一台装着韧传的电脑，或者一台已经打开过韧传网页的手机。",
            )
            return
        labels = [f"{name}（{kind}）" for _id, name, kind in members]
        choice, ok = QInputDialog.getItem(self, "和谁聊天", "选择设备", labels, 0, False)
        if not ok or not choice:
            return
        member_id, name, kind = members[labels.index(choice)]
        if kind == "手机":
            conv_id = direct_conversation_id(self.engine.info.device_id, member_id)
            self.engine.chat.upsert_conversation(
                conv_id, kind="direct", title=f"和 {name} 的对话", members=[self.engine.info.device_id, member_id]
            )
        else:
            conv_id = direct_conversation_id(self.engine.info.device_id, member_id)
            self.engine.chat.upsert_conversation(
                conv_id, kind="direct", title=f"和 {name} 的对话", members=[self.engine.info.device_id, member_id]
            )
        self.reload()
        self._select_conversation(conv_id)

    def _new_group(self) -> None:
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
        chosen = [picker.item(i).data(Qt.UserRole) for i in range(picker.count()) if picker.item(i).isSelected()]
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
        self._send(conv_id, "system", f"{self.engine.info.name} 创建了群聊「{title}」", members=[self.engine.info.device_id, *chosen], title=title)
        self.reload()
        self._select_conversation(conv_id)

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

    def _send_text(self) -> None:
        text = self.input.text().strip()
        if not text or not self._conversation:
            if not self._conversation:
                QMessageBox.information(self, "还没有选择会话", "先在左边选一个会话，或点「发起聊天」。")
            return
        self.input.clear()
        self._send(self._conversation, "text", text)

    def send_attachment(self, path: str) -> None:
        if not self._conversation:
            QMessageBox.information(self, "还没有选择会话", "先在左边选一个会话，或点「发起聊天」。")
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

        threading.Thread(target=run, name="chat-send", daemon=True).start()


class ImageViewer(QDialog):
    """Full-size look at one picture, with a way to save or open it.

    Deliberately simple: the chat bubble already proves the image arrived, and
    this is for the times you actually need to read what is in it.  Clicking
    the picture toggles between "fit the window" and "actual pixels", because
    a screenshot of a bug report is unreadable when scaled down to 800 px.
    """

    def __init__(self, path: str, name: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.path = path
        self._original = QPixmap(path)
        self._full_size = False
        self.setWindowTitle(name)
        self.resize(900, 680)

        layout = QVBoxLayout(self)
        self.canvas = QLabel()
        self.canvas.setAlignment(Qt.AlignCenter)
        self.canvas.setMinimumSize(320, 240)
        self.canvas.setStyleSheet("background: #101418; border-radius: 8px;")
        self.canvas.setCursor(Qt.PointingHandCursor)
        self.canvas.mousePressEvent = self._toggle  # type: ignore[method-assign]
        layout.addWidget(self.canvas, 1)

        size = self._original.size()
        hint = QLabel(
            f"{name} · {size.width()}×{size.height()} · "
            f"{human_bytes(os.path.getsize(path))}　（点击图片切换原始大小）"
        )
        hint.setObjectName("Subtitle")
        layout.addWidget(hint)

        buttons = QDialogButtonBox()
        save = buttons.addButton("另存为…", QDialogButtonBox.ActionRole)
        save.clicked.connect(self._save_as)
        external = buttons.addButton("用系统看图工具打开", QDialogButtonBox.ActionRole)
        external.clicked.connect(lambda: platform_open.open_path(self.path))
        close = buttons.addButton("关闭", QDialogButtonBox.RejectRole)
        close.clicked.connect(self.reject)
        layout.addWidget(buttons)
        self._fit()

    def _fit(self) -> None:
        if self._full_size:
            self.canvas.setPixmap(self._original)
            self.canvas.setScaledContents(False)
        else:
            self.canvas.setPixmap(
                self._original.scaled(
                    max(320, self.canvas.width()),
                    max(240, self.canvas.height()),
                    Qt.KeepAspectRatio,
                    Qt.SmoothTransformation,
                )
            )

    def _toggle(self, _event) -> None:
        self._full_size = not self._full_size
        self._fit()

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt naming
        super().resizeEvent(event)
        if not self._full_size:
            self._fit()

    def _save_as(self) -> None:
        target, _filter = QFileDialog.getSaveFileName(
            self, "另存为", os.path.basename(self.path)
        )
        if not target:
            return
        try:
            with open(self.path, "rb") as src, open(target, "wb") as dst:
                dst.write(src.read())
        except OSError as exc:
            QMessageBox.warning(self, "保存失败", str(exc))
            return
        QMessageBox.information(self, "已保存", f"已保存到 {target}")


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
