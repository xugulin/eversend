"""Reusable widgets: drop zone, device list, transfer rows, QR panel."""

from __future__ import annotations

import os
import urllib.parse
from typing import Iterable

from PySide6.QtCore import QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QDialog,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..core import qr
from ..core.model import (
    DeviceInfo,
    Peer,
    TransferItem,
    human_bytes,
    human_duration,
    human_speed,
)
from . import theme


class DropZone(QFrame):
    """A panel that accepts dropped files and folders."""

    paths_dropped = Signal(list)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("DropZone")
        self.setAcceptDrops(True)
        self.setMinimumHeight(120)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

        layout = QVBoxLayout(self)
        layout.setAlignment(Qt.AlignCenter)
        layout.setSpacing(6)

        self.icon = QLabel("📂")
        font = QFont()
        font.setPointSize(28)
        self.icon.setFont(font)
        self.icon.setAlignment(Qt.AlignCenter)

        self.title = QLabel("把文件或文件夹拖到这里")
        self.title.setAlignment(Qt.AlignCenter)
        self.title.setObjectName("Title")

        self.hint = QLabel("或者点击下面的「添加文件」「添加文件夹」")
        self.hint.setAlignment(Qt.AlignCenter)
        self.hint.setObjectName("Subtitle")

        layout.addWidget(self.icon)
        layout.addWidget(self.title)
        layout.addWidget(self.hint)

    def _set_active(self, active: bool) -> None:
        self.setProperty("active", "true" if active else "false")
        # A property change only takes effect after the style is re-evaluated.
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


class DeviceTable(QTableWidget):
    """The list of discovered peers."""

    COLUMNS = ("设备", "类型", "地址", "状态")

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(0, len(self.COLUMNS), parent)
        self.setHorizontalHeaderLabels(self.COLUMNS)
        self.verticalHeader().setVisible(False)
        self.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.setSelectionMode(QAbstractItemView.SingleSelection)
        self.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.setShowGrid(False)
        self.setAlternatingRowColors(False)
        header = self.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        for column in range(1, len(self.COLUMNS)):
            header.setSectionResizeMode(column, QHeaderView.ResizeToContents)
        self._peers: list[Peer] = []

    @property
    def peers(self) -> list[Peer]:
        return list(self._peers)

    def selected_peer(self) -> Peer | None:
        row = self.currentRow()
        if 0 <= row < len(self._peers):
            return self._peers[row]
        return None

    def select_first(self) -> None:
        if self._peers and self.currentRow() < 0:
            self.selectRow(0)

    def set_peers(self, peers: list[Peer]) -> None:
        selected_key = None
        current = self.selected_peer()
        if current is not None:
            selected_key = current.key

        self._peers = list(peers)
        self.setRowCount(len(self._peers))
        for row, peer in enumerate(self._peers):
            info: DeviceInfo = peer.info
            name = QTableWidgetItem(f"{theme.platform_glyph(info.platform)}  {info.name}")
            if peer.trusted:
                name.setToolTip("已信任的设备")
            elif peer.same_host:
                name.setToolTip(
                    "这台设备就在本机上——通常是又开了一个 EverSend 实例。\n"
                    "如果不是你有意开的，可以把那个多余的窗口关掉。"
                )
            kind = QTableWidgetItem(_kind_label(info))
            address = QTableWidgetItem(f"{peer.address}:{peer.port}")
            status = QTableWidgetItem("已信任" if peer.trusted else "在线")
            if peer.trusted:
                status.setForeground(QColor(theme.SUCCESS))
            for column, item in enumerate((name, kind, address, status)):
                self.setItem(row, column, item)

        if selected_key is not None:
            for row, peer in enumerate(self._peers):
                if peer.key == selected_key:
                    self.selectRow(row)
                    break
        else:
            self.select_first()


def _kind_label(info: DeviceInfo) -> str:
    labels = {"desktop": "电脑", "mobile": "手机", "server": "服务器"}
    kind = labels.get(info.kind, info.kind)
    return f"{kind} · {info.platform}" if info.platform else kind


class TransferRow(QFrame):
    """One live transfer: name, progress bar, speed/ETA and a cancel button."""

    cancel_requested = Signal(str)

    def __init__(self, transfer_id: str, title: str, direction: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("Card")
        self.transfer_id = transfer_id
        self.direction = direction

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(6)

        top = QHBoxLayout()
        arrow = "⬆" if direction == "send" else "⬇"
        self.title = QLabel(f"{arrow}  {title}")
        self.title.setObjectName("Title")
        top.addWidget(self.title, 1)

        self.status = QLabel("准备中…")
        self.status.setObjectName("Subtitle")
        top.addWidget(self.status)

        self.cancel_button = QPushButton("取消")
        self.cancel_button.setFixedWidth(70)
        self.cancel_button.clicked.connect(lambda: self.cancel_requested.emit(self.transfer_id))
        top.addWidget(self.cancel_button)
        layout.addLayout(top)

        self.bar = QProgressBar()
        self.bar.setRange(0, 1000)
        self.bar.setValue(0)
        self.bar.setFormat("%p%")
        layout.addWidget(self.bar)

        self.detail = QLabel("")
        self.detail.setObjectName("Subtitle")
        layout.addWidget(self.detail)

    def update_progress(self, done: int, total: int, speed: float, eta: float | None) -> None:
        total = max(1, total)
        permille = int(1000 * min(done, total) / total)
        self.bar.setValue(permille)
        self.bar.setFormat(f"{100.0 * min(done, total) / total:.1f}%")
        parts = [f"{human_bytes(done)} / {human_bytes(total)}"]
        if speed > 0:
            parts.append(human_speed(speed))
        if eta is not None and eta > 0:
            parts.append(f"剩余 {human_duration(eta)}")
        self.detail.setText("   ·   ".join(parts))

    def set_status(self, text: str, color: str = "") -> None:
        self.status.setText(text)
        if color:
            self.status.setStyleSheet(f"color: {color};")

    def finish(self, ok: bool, message: str = "") -> None:
        self.bar.setValue(1000 if ok else self.bar.value())
        self.set_status(message or ("完成" if ok else "失败"), theme.SUCCESS if ok else theme.DANGER)
        self.cancel_button.setEnabled(False)
        self.cancel_button.setText("关闭")
        try:
            self.cancel_button.clicked.disconnect()
        except RuntimeError:
            pass
        self.cancel_button.clicked.connect(self.deleteLater)


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


class FileListWidget(QTableWidget):
    """The list of files queued for sending."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(0, 2, parent)
        self.setHorizontalHeaderLabels(("文件", "大小"))
        self.verticalHeader().setVisible(False)
        self.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.setShowGrid(False)
        header = self.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.paths: list[str] = []

    def add_paths(self, paths: Iterable[str]) -> int:
        added = 0
        for path in paths:
            absolute = os.path.abspath(path)
            if absolute in self.paths or not os.path.exists(absolute):
                continue
            self.paths.append(absolute)
            size = _tree_size(absolute)
            row = self.rowCount()
            self.insertRow(row)
            name = QTableWidgetItem(os.path.basename(absolute) or absolute)
            name.setToolTip(absolute)
            self.setItem(row, 0, name)
            self.setItem(row, 1, QTableWidgetItem(human_bytes(size)))
            added += 1
        return added

    def remove_selected(self) -> None:
        rows = sorted({index.row() for index in self.selectedIndexes()}, reverse=True)
        for row in rows:
            if 0 <= row < len(self.paths):
                self.paths.pop(row)
                self.removeRow(row)

    def clear_all(self) -> None:
        self.paths.clear()
        self.setRowCount(0)

    def total_size(self) -> int:
        return sum(_tree_size(path) for path in self.paths)


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
        layout.setSpacing(10)

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
    "DeviceTable",
    "DropZone",
    "FileListWidget",
    "OfferDialog",
    "QrDialog",
    "TransferRow",
    "render_qr",
]
