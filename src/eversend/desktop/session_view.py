"""The main area: 对方设备详情在顶、会话或传输在中间、输入区在会话底部。

It is one widget so the two views share the same header — the ``[会话｜传输]``
switch lives there, which is what makes the transfer table reachable from
inside a session (and the sidebar's 「传输」 item reaches the same page: see
``MainWindow.show_page``).

拖放也落在这里：把文件拖到主区**任意位置**（顶上那条设备详情、会话气泡之间、
传输表上都算）都会加入待发 —— 规格里"拖放到会话区任意位置 = 加入待发"
（``docs/UI_LAYOUT.md`` §3⑥）。拖进来的时候给一层提示，让用户知道松手会发生
什么，而不是"拖了没反应"。
"""

from __future__ import annotations

import os

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QLabel, QStackedWidget, QVBoxLayout, QWidget

from .peer_header import PeerHeader


class SessionView(QWidget):
    """顶部对方设备详情 + 中间会话/传输 + 底部输入区（输入区在会话页里）。"""

    paths_dropped = Signal(list)

    def __init__(self, chat: QWidget, transfers: QWidget, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.chat = chat
        self.transfers = transfers

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self.header = PeerHeader()
        self.header.view_changed.connect(self.show_view)
        root.addWidget(self.header)

        self.stack = QStackedWidget()
        self.stack.addWidget(chat)
        self.stack.addWidget(transfers)
        root.addWidget(self.stack, 1)

        # 拖放提示：默认藏着，拖进来才出现（不占平时的高度）。
        self.drop_hint = QLabel("松开就把这些文件加入待发（回车或点「发送」发出去）", self.stack)
        self.drop_hint.setObjectName("DropHint")
        self.drop_hint.setAlignment(Qt.AlignCenter)
        self.drop_hint.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.drop_hint.setVisible(False)

    # -- view switching ----------------------------------------------------

    def show_view(self, key: str) -> None:
        """``chat`` / ``transfers`` —— 头部与主区永远显示同一件事。"""
        self.header.set_view(key)
        self.stack.setCurrentWidget(self.transfers if key == "transfers" else self.chat)

    def current_view(self) -> str:
        return "transfers" if self.stack.currentWidget() is self.transfers else "chat"

    def view_widget(self, key: str) -> QWidget:
        return self.transfers if key == "transfers" else self.chat

    # -- drag & drop: 拖进会话 = 加入待发文件 --------------------------------

    def _set_drop_hint(self, visible: bool) -> None:
        self.drop_hint.setVisible(visible)
        if visible:
            self.drop_hint.resize(self.stack.size())
            self.drop_hint.move(0, 0)
            self.drop_hint.raise_()

    def _paths_from(self, event) -> list[str]:
        if not event.mimeData().hasUrls():
            return []
        paths: list[str] = []
        for url in event.mimeData().urls():
            local = url.toLocalFile()
            if local and os.path.exists(local):
                paths.append(local)
        return paths

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt naming
        super().resizeEvent(event)
        if self.drop_hint.isVisible():
            self.drop_hint.resize(self.stack.size())

    def dragEnterEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
            self._set_drop_hint(True)

    def dragMoveEvent(self, event) -> None:  # noqa: N802
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
            self._set_drop_hint(True)

    def dragLeaveEvent(self, event) -> None:  # noqa: N802
        self._set_drop_hint(False)
        super().dragLeaveEvent(event)

    def dropEvent(self, event) -> None:  # noqa: N802
        self._set_drop_hint(False)
        paths = self._paths_from(event)
        if paths:
            self.paths_dropped.emit(paths)
            event.acceptProposedAction()

    # -- headers -----------------------------------------------------------

    def set_peer(self, peer) -> None:
        """把设备详情交给头部（主区顶部那一块）。"""
        self.header.set_peer(peer)


__all__ = ["SessionView"]
