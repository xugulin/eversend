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

import os
import subprocess
import sys
import threading
import time
from typing import Any

from PySide6.QtCore import Qt, QTimer, Signal
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

from ..core import platform_open
from ..core.chat import MEDIA_KINDS, direct_conversation_id
from ..core.engine import Engine
from ..core.model import human_bytes

#: A small palette is enough: the picker is for the emoji people actually send,
#: not for a full Unicode browser.
EMOJI = [
    "😀", "😂", "🥹", "😊", "😍", "😘", "🤔", "😴",
    "😎", "🤩", "😭", "😅", "🙃", "😇", "🥳", "🤝",
    "👍", "👎", "👌", "🙏", "👏", "💪", "🤙", "✌️",
    "❤️", "💔", "🔥", "✨", "🎉", "🎁", "⭐", "💡",
    "✅", "❌", "⚠️", "❓", "❗", "📎", "📷", "🎬",
    "🎵", "🎤", "💻", "📱", "🖥️", "📁", "📄", "🗑️",
]


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
        self._build()
        self._tick = QTimer(self)
        self._tick.setInterval(1500)
        self._tick.timeout.connect(self.reload)
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
        for conversation in conversations:
            item = QListWidgetItem(self._conversation_label(conversation))
            item.setData(Qt.UserRole, conversation["id"])
            self.conv_list.addItem(item)
        self.conv_list.blockSignals(False)
        self._select_conversation(self._conversation or self._first_id())

    @staticmethod
    def _conversation_label(conversation: dict[str, Any]) -> str:
        badge = "  🔴%d" % conversation["unread"] if conversation.get("unread") else ""
        kind = "群" if conversation.get("kind") == "group" else "一对一"
        last = str(conversation.get("lastText") or "")
        if len(last) > 28:
            last = last[:28] + "…"
        return f"{conversation.get('title') or conversation['id'][:12]}｜{kind}{badge}\n{last}"

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
            + f"成员：{self.engine.info.name}"
            + ("、" + "、".join(others) if others else "")
        )
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

    def _member_label(self, member: str) -> str:
        if member.startswith("web:"):
            key = member[4:]
            for client in getattr(self, "_clients_provider", lambda: [])() or []:
                if client.get("key") == key:
                    return f"{client.get('label') or '手机'}（手机）"
            return "手机（浏览器）"
        for peer in self.engine.devices():
            if peer.info.device_id == member:
                return peer.info.name
        return member[:10]

    def _clear_messages(self) -> None:
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
                label.setToolTip(name)
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
            layout.addWidget(QLabel(f"{icon}  {name}   {detail}"))

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
        menu = QMenu(self)
        grid = QWidget()
        layout = QVBoxLayout(grid)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(2)
        row_layout = None
        for index, emoji in enumerate(EMOJI):
            if index % 8 == 0:
                row_layout = QHBoxLayout()
                row_layout.setSpacing(2)
                layout.addLayout(row_layout)
            button = QPushButton(emoji)
            button.setFixedSize(34, 30)
            button.setFlat(True)
            button.clicked.connect(lambda _=False, e=emoji: self._insert_emoji(e))
            row_layout.addWidget(button)
        action = menu.addAction("常用表情")
        action.setEnabled(False)
        menu.layout().addWidget(grid) if hasattr(menu, "layout") else None
        menu.addSeparator()
        more = menu.addAction("更多…（自己输入）")
        more.triggered.connect(lambda: self.input.setFocus())
        menu.exec(self.emoji_button.mapToGlobal(self.emoji_button.rect().bottomLeft()))
        grid.setParent(None)

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


__all__ = ["ChatView", "EMOJI", "guess_kind"]
