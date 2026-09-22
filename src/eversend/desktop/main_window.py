"""The main application window."""

from __future__ import annotations

import hashlib
import json
import os
import time

from PySide6.QtCore import Qt, QTimer, Signal, Slot
from PySide6.QtGui import QAction, QGuiApplication
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QStatusBar,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ..core.engine import Engine, EngineConfig
from ..core.model import DeviceInfo, Peer, human_bytes
from ..core.sockutil import list_interfaces
from . import theme
from .bridge import EngineBridge, SendWorker
from .chat_view import ChatView
from .widgets import (
    DeviceTable,
    DropZone,
    FileListWidget,
    OfferDialog,
    QrDialog,
    TransferRow,
)


def form_label(text: str) -> QLabel:
    """A form label with a width floor.

    ``QFormLayout`` sizes its label column from the widgets' size hints, and
    Qt's default hint for CJK text is narrower than the text actually renders,
    so labels like 传输端口 come out elided.  Reserving the width up front is
    simpler and more predictable than fighting the layout.
    """
    label = QLabel(text)
    label.setMinimumWidth(96)
    label.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
    label.setWordWrap(False)
    return label


#: How long a phone stays listed after its last request.  A browser that is
#: asleep is not gone: the handed-off files are still waiting for it.
PHONE_OFFLINE_GRACE = 300.0

#: A phone that has not been heard from for this long is shown as 已离线.
ONLINE_WINDOW = 12.0


class MainWindow(QMainWindow):
    """EverSend's desktop window."""

    #: Emitted by a SendWorker on its own thread; Qt queues it onto the GUI
    #: thread because this object lives there.  (ok, error, pending_key)
    send_finished = Signal(bool, str, str)

    def __init__(self, engine: Engine, settings_path: str = "") -> None:
        super().__init__()
        self.engine = engine
        self.settings_path = settings_path
        self.bridge = EngineBridge(engine, self)
        self._transfers: dict[str, TransferRow] = {}
        #: transfer id -> the placeholder key its card was created under, so a
        #: send can be finished no matter which of the two names is used.
        self._row_aliases: dict[str, str] = {}
        self._send_workers: list[SendWorker] = []
        self._offer_dialogs: dict[str, OfferDialog] = {}
        self._last_tick = time.monotonic()
        self._last_bytes: dict[str, int] = {}
        #: share id -> 传输行，用于显示"手机已经取走"。
        self._phone_shares: dict[str, TransferRow] = {}

        self.setWindowTitle("韧传 EverSend")
        self.resize(1080, 760)
        self.setMinimumSize(880, 620)

        #: Set by :meth:`on_web_ui_started`; ``None`` until (and unless) the
        #: browser interface is up.
        self._web_ui = None
        #: HTTPS port, when the secure copy of the page started (0 = none).
        self._web_tls_port = 0
        #: Devices seen since the current scan started, and the backstop timer.
        self._scan_hits = 0
        self._scan_watchdog: QTimer | None = None

        self._build_ui()
        self._connect_bridge()

        self._tick = QTimer(self)
        self._tick.setInterval(400)
        self._tick.timeout.connect(self._refresh_progress)
        self._tick.start()

        self._discovery_tick = QTimer(self)
        self._discovery_tick.setInterval(2000)
        self._discovery_tick.timeout.connect(self._refresh_devices)
        self._discovery_tick.start()

        # Same cadence as the device list: the phone's page polls us every few
        # seconds, so this shows it within a couple of seconds of it connecting
        # (and stops showing it a few seconds after it leaves).
        self._clients_tick = QTimer(self)
        self._clients_tick.setInterval(2000)
        self._clients_tick.timeout.connect(self._refresh_web_clients)
        self._clients_tick.start()

        self._refresh_devices()
        self._refresh_web_clients()
        self._update_header()

    # ------------------------------------------------------------------
    # construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(16, 14, 16, 10)
        root.setSpacing(12)

        root.addWidget(self._build_header())

        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_send_tab(), "发送")
        self.tabs.addTab(self._build_receive_tab(), "接收")
        self.tabs.addTab(self._build_transfers_tab(), "传输")
        self.chat_view = ChatView(self.engine)
        self.chat_view.send_finished.connect(self._on_chat_sent)
        self.tabs.addTab(self.chat_view, "聊天")
        self.tabs.addTab(self._build_settings_tab(), "设置")
        root.addWidget(self.tabs, 1)

        status = QStatusBar()
        self.setStatusBar(status)
        self.status_left = QLabel("正在启动…")
        self.status_right = QLabel("")
        status.addWidget(self.status_left, 1)
        status.addPermanentWidget(self.status_right)

    def _build_header(self) -> QWidget:
        frame = QFrame()
        frame.setObjectName("Card")
        layout = QHBoxLayout(frame)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(14)

        left = QVBoxLayout()
        left.setSpacing(2)
        self.device_name_label = QLabel("本机")
        self.device_name_label.setObjectName("Title")
        self.device_address_label = QLabel("")
        self.device_address_label.setObjectName("Subtitle")
        self.device_address_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        # 手机要填的是**网页端口**，而上面那行是传输端口。用户照着传输地址往
        # App 里填，得到的只会是 "unexpected end of stream"（HTTP 打到二进制
        # 传输端口上）—— 所以把手机该填的那一行也放在这里，一眼能看到。
        self.phone_url_label = QLabel("")
        self.phone_url_label.setObjectName("Subtitle")
        self.phone_url_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        left.addWidget(self.device_name_label)
        left.addWidget(self.device_address_label)
        left.addWidget(self.phone_url_label)
        layout.addLayout(left, 1)

        self.encryption_label = QLabel("")
        self.encryption_label.setObjectName("Subtitle")
        layout.addWidget(self.encryption_label)

        self.qr_button = QPushButton("📱 手机连接")
        self.qr_button.setObjectName("Primary")
        self.qr_button.clicked.connect(self._show_qr)
        layout.addWidget(self.qr_button)

        self.announce_button = QPushButton("重新搜索")
        self.announce_button.clicked.connect(self._announce)
        layout.addWidget(self.announce_button)

        return frame

    def _build_send_tab(self) -> QWidget:
        page = QWidget()
        layout = QHBoxLayout(page)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(14)

        # -- device column --------------------------------------------------
        left = QVBoxLayout()
        left.setSpacing(8)
        title_row = QHBoxLayout()
        devices_title = QLabel("选择接收设备")
        devices_title.setObjectName("Title")
        title_row.addWidget(devices_title)
        title_row.addStretch(1)
        # 手动刷新：设备列表平时靠发现事件自己更新，但"我明明开着它却没出现"
        # 的时候，用户需要有一个按钮可以立刻重来一遍，而不是等 30 秒的公告。
        self.refresh_button = QPushButton("刷新")
        self.refresh_button.setToolTip("立刻重新读取设备列表并催一次公告（不用等自动发现）")
        self.refresh_button.clicked.connect(self._refresh_devices_now)
        title_row.addWidget(self.refresh_button)
        left.addLayout(title_row)

        self.device_table = DeviceTable()
        self.device_table.doubleClicked.connect(lambda _index: self._pick_files())
        self.device_table.remove_requested.connect(self._remove_device)
        # Multi-select changes what the 发送 button will do, so the label has to
        # follow the selection rather than only the device list.
        self.device_table.itemSelectionChanged.connect(self._update_send_button)
        left.addWidget(self.device_table, 1)

        device_buttons = QHBoxLayout()
        self.scan_button = QPushButton("扫描局域网")
        self.scan_button.setToolTip("有些路由器会拦掉广播，扫描会逐个探测网段里的设备")
        self.scan_button.clicked.connect(self._scan)
        device_buttons.addWidget(self.scan_button)

        self.manual_button = QPushButton("手动添加 IP")
        self.manual_button.clicked.connect(self._manual_add)
        device_buttons.addWidget(self.manual_button)
        left.addLayout(device_buttons)

        left_widget = QWidget()
        left_widget.setLayout(left)
        layout.addWidget(left_widget, 5)

        # -- file column ----------------------------------------------------
        right = QVBoxLayout()
        right.setSpacing(8)
        files_title = QLabel("要发送的内容")
        files_title.setObjectName("Title")
        right.addWidget(files_title)

        self.drop_zone = DropZone()
        self.drop_zone.paths_dropped.connect(self._add_paths)
        right.addWidget(self.drop_zone)

        self.file_list = FileListWidget()
        self.file_list.setMinimumHeight(140)
        right.addWidget(self.file_list, 1)

        file_buttons = QHBoxLayout()
        add_files = QPushButton("添加文件")
        add_files.clicked.connect(self._pick_files)
        file_buttons.addWidget(add_files)
        add_dir = QPushButton("添加文件夹")
        add_dir.clicked.connect(self._pick_directory)
        file_buttons.addWidget(add_dir)
        remove = QPushButton("移除选中")
        remove.clicked.connect(self._remove_selected)
        file_buttons.addWidget(remove)
        clear = QPushButton("清空")
        clear.clicked.connect(self._clear_files)
        file_buttons.addWidget(clear)
        right.addLayout(file_buttons)

        self.total_label = QLabel("尚未选择文件")
        self.total_label.setObjectName("Subtitle")
        right.addWidget(self.total_label)

        send_row = QHBoxLayout()
        self.pin_edit = QLineEdit()
        self.pin_edit.setPlaceholderText("接收方 PIN（如果对方设置了）")
        self.pin_edit.setMaxLength(32)
        send_row.addWidget(self.pin_edit, 1)
        self.send_button = QPushButton("发送")
        self.send_button.setToolTip("Ctrl / Shift 点选可以一次发给多台设备")
        self.send_button.setObjectName("Primary")
        self.send_button.setMinimumWidth(120)
        self.send_button.clicked.connect(self._send)
        send_row.addWidget(self.send_button)
        right.addLayout(send_row)

        right_widget = QWidget()
        right_widget.setLayout(right)
        layout.addWidget(right_widget, 6)

        return page

    def _build_receive_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(10)

        top = QHBoxLayout()
        title = QLabel("接收到的文件")
        title.setObjectName("Title")
        top.addWidget(title, 1)

        self.receive_dir_label = QLabel("")
        self.receive_dir_label.setObjectName("Subtitle")
        top.addWidget(self.receive_dir_label)

        open_dir = QPushButton("打开文件夹")
        open_dir.clicked.connect(self._open_receive_dir)
        top.addWidget(open_dir)

        change_dir = QPushButton("更改位置")
        change_dir.clicked.connect(self._change_receive_dir)
        top.addWidget(change_dir)
        layout.addLayout(top)

        hint = QLabel(
            "有人向你发送文件时会自动弹出确认窗口。勾选「信任这台设备」后，"
            "以后来自它的传输会自动接收。"
        )
        hint.setObjectName("Subtitle")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        self.history_container = QWidget()
        self.history_layout = QVBoxLayout(self.history_container)
        self.history_layout.setContentsMargins(0, 0, 0, 0)
        self.history_layout.setSpacing(6)
        self.history_layout.addStretch(1)
        scroll.setWidget(self.history_container)
        layout.addWidget(scroll, 1)

        return page

    def _build_transfers_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(10)

        self.transfers_empty = QLabel("当前没有正在进行的传输")
        self.transfers_empty.setObjectName("Subtitle")
        self.transfers_empty.setAlignment(Qt.AlignCenter)
        layout.addWidget(self.transfers_empty)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        self.transfers_container = QWidget()
        self.transfers_layout = QVBoxLayout(self.transfers_container)
        self.transfers_layout.setContentsMargins(0, 0, 0, 0)
        self.transfers_layout.setSpacing(8)
        self.transfers_layout.addStretch(1)
        scroll.setWidget(self.transfers_container)
        layout.addWidget(scroll, 1)

        return page

    def _build_settings_tab(self) -> QWidget:
        page = QWidget()
        outer = QVBoxLayout(page)
        outer.setContentsMargins(14, 14, 14, 14)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        inner = QWidget()
        layout = QVBoxLayout(inner)
        layout.setSpacing(14)

        # -- identity -------------------------------------------------------
        identity = QFrame()
        identity.setObjectName("Card")
        form = QFormLayout(identity)
        form.setContentsMargins(16, 14, 16, 14)
        form.setSpacing(10)
        form.setLabelAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)

        self.name_edit = QLineEdit(self.engine.config.name)
        self.name_edit.setMaxLength(48)
        form.addRow(form_label("设备名称"), self.name_edit)

        self.device_id_label = QLabel(self.engine.identity.device_id)
        self.device_id_label.setObjectName("Mono")
        self.device_id_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        form.addRow(form_label("设备 ID"), self.device_id_label)

        self.port_label = QLabel(str(self.engine.port or "（启动中）"))
        form.addRow(form_label("传输端口"), self.port_label)
        layout.addWidget(identity)

        # -- transfer behaviour ---------------------------------------------
        behaviour = QFrame()
        behaviour.setObjectName("Card")
        form2 = QFormLayout(behaviour)
        form2.setContentsMargins(16, 14, 16, 14)
        form2.setSpacing(10)
        form2.setLabelAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        form2.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)

        self.streams_spin = QSpinBox()
        self.streams_spin.setRange(1, 16)
        self.streams_spin.setValue(self.engine.config.streams)
        self.streams_spin.setToolTip(
            "并行连接数。千兆网卡 4 条足够；2.5G/万兆或高延迟链路可以调到 8。\n"
            "机械硬盘上会自动降到 2 条，因为随机写入反而更慢。"
        )
        form2.addRow(form_label("并行连接数"), self.streams_spin)

        self.pin_field = QLineEdit(self.engine.config.pin)
        self.pin_field.setPlaceholderText("留空表示不需要 PIN")
        self.pin_field.setMaxLength(32)
        self.pin_field.setToolTip("设置后，发送方必须输入相同的 PIN 才能传输。")
        form2.addRow(form_label("接收 PIN"), self.pin_field)

        self.auto_trust_box = QCheckBox("自动接收已信任设备的文件")
        self.auto_trust_box.setChecked(self.engine.config.auto_accept_trusted)
        form2.addRow(self.auto_trust_box)

        self.auto_all_box = QCheckBox("自动接收所有设备的文件（不安全）")
        self.auto_all_box.setChecked(self.engine.config.auto_accept_all)
        form2.addRow(self.auto_all_box)

        self.encrypt_box = QCheckBox("加密传输内容")
        self.encrypt_box.setChecked(self.engine.config.encrypt)
        self.encrypt_box.setEnabled(False)
        self.encrypt_box.setToolTip("加密设置在启动时确定，修改后需要重启。")
        form2.addRow(self.encrypt_box)

        self.resume_box = QCheckBox("支持断点续传")
        self.resume_box.setChecked(self.engine.config.resume)
        self.resume_box.setToolTip("关闭后每次重新传输都从头开始，一般不需要关闭。")
        form2.addRow(self.resume_box)

        layout.addWidget(behaviour)

        # -- browser ui -----------------------------------------------------
        web = QFrame()
        web.setObjectName("Card")
        form3 = QFormLayout(web)
        form3.setContentsMargins(16, 14, 16, 14)
        form3.setSpacing(10)
        form3.setLabelAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        form3.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)

        self.web_url_label = QLabel("")
        self.web_url_label.setObjectName("Mono")
        self.web_url_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.web_url_label.setWordWrap(True)
        form3.addRow(form_label("手机访问地址"), self.web_url_label)

        web_hint = QLabel(
            "手机、平板不用装任何软件：连同一个 Wi-Fi，用浏览器打开上面的地址，"
            "或者点右上角「手机连接」扫码。"
        )
        web_hint.setObjectName("Subtitle")
        web_hint.setWordWrap(True)
        form3.addRow(web_hint)

        # Phones are browser clients, so they never appear in the peer list on
        # the left -- which made 「手机连接」 look like it had done nothing.
        # This is the line that answers "did my phone actually connect?".
        self.web_clients_label = QLabel("")
        self.web_clients_label.setObjectName("Subtitle")
        self.web_clients_label.setWordWrap(True)
        form3.addRow(form_label("已连接的手机"), self.web_clients_label)
        layout.addWidget(web)

        save = QPushButton("保存设置")
        save.setObjectName("Primary")
        save.clicked.connect(self._save_settings)
        layout.addWidget(save, alignment=Qt.AlignLeft)

        layout.addStretch(1)
        scroll.setWidget(inner)
        outer.addWidget(scroll)
        return page

    # ------------------------------------------------------------------
    # bridge wiring
    # ------------------------------------------------------------------

    def _connect_bridge(self) -> None:
        self.bridge.device_found.connect(self._on_device)
        self.bridge.device_updated.connect(self._on_device)
        self.bridge.scan_hit.connect(self._on_scan_hit)
        self.bridge.scan_finished.connect(self._on_scan_finished)
        self.bridge.chat_changed.connect(self._on_chat_changed)
        self.bridge.offer_received.connect(self._on_offer)
        self.bridge.transfer_finished.connect(self._on_transfer_finished)
        self.bridge.warning.connect(self._on_warning)
        self.bridge.engine_started.connect(self._on_engine_started)
        self.bridge.file_done.connect(self._on_file_done)
        self.send_finished.connect(self._on_send_finished)

    @Slot(object)
    def _on_device(self, _peer) -> None:
        self._refresh_devices()

    @Slot(str, int)
    def _on_scan_hit(self, address: str, port: int) -> None:
        self._scan_hits = getattr(self, "_scan_hits", 0) + 1
        """A host answered a scan probe; find out *who* it is.

        The probe only proved that something is listening.  Registering that
        as a device would key it by IP, which can never merge with the same
        machine's real record -- so after every scan the list grew a second,
        useless row per device.  Instead we do a handshake off the GUI thread
        and adopt the peer's real identity (or ignore it, if it is not us).
        """
        import threading

        def probe() -> None:
            try:
                self.engine.probe_address(address, port)
            except Exception:
                pass

        threading.Thread(
            target=probe, name=f"probe-{address}", daemon=True
        ).start()

    @Slot(dict)
    def _on_offer(self, event: dict) -> None:
        request_id = str(event.get("request_id", ""))
        if not request_id:
            return
        dialog = OfferDialog(event, self)
        dialog.setModal(False)
        self._offer_dialogs[request_id] = dialog
        dialog.finished.connect(lambda _r, rid=request_id, d=dialog: self._resolve_offer(rid, d))
        dialog.show()
        self.activateWindow()
        dialog.raise_()

    def _resolve_offer(self, request_id: str, dialog: OfferDialog) -> None:
        self._offer_dialogs.pop(request_id, None)
        try:
            self.engine.resolve_offer(
                request_id,
                bool(dialog.accept_transfer),
                trust=bool(dialog.trust_device),
            )
        except Exception as exc:
            QMessageBox.warning(self, "无法处理该请求", str(exc))

    @Slot(dict)
    def _on_transfer_finished(self, event: dict) -> None:
        transfer_id = str(event.get("transfer_id", ""))
        status = str(event.get("status", ""))
        error = str(event.get("error", ""))
        ok = status in ("done", "finished")
        message = error or ("完成" if ok else ("已取消" if status == "cancelled" else "已失败"))
        self._finish_row(transfer_id, ok, message)
        if ok:
            self.status_left.setText(
                f"传输完成：{human_bytes(int(event.get('bytes', 0)))}"
            )
        elif error:
            self.status_left.setText(f"传输失败：{error}")

    @Slot(str, int, str, str)
    def _on_file_done(self, transfer_id: str, _index: int, name: str, path: str) -> None:
        self._add_history(name, path, transfer_id)

    @Slot(int)
    def _on_engine_started(self, port: int) -> None:
        self.port_label.setText(str(port))
        self._update_header()

    @Slot(str)
    def _on_warning(self, message: str) -> None:
        self.status_left.setText(f"提示：{message}")

    # ------------------------------------------------------------------
    # actions
    # ------------------------------------------------------------------

    def _update_header(self) -> None:
        info = self.engine.info
        self.device_name_label.setText(info.name)
        port = self.engine.port
        addresses = []
        if port:
            for iface in list_interfaces(include_virtual=False):
                host = f"[{iface.address}]" if iface.is_ipv6 else iface.address
                addresses.append(f"{host}:{port}")
        self.device_address_label.setText(
            "  ·  ".join(addresses[:3]) if addresses else "正在检测网络…"
        )

        from ..core import crypto

        if crypto.CRYPTO_AVAILABLE and self.engine.config.encrypt:
            self.encryption_label.setText("🔒 传输已加密")
            self.encryption_label.setStyleSheet(f"color: {theme.SUCCESS};")
        else:
            self.encryption_label.setText("⚠ 未加密")
            self.encryption_label.setStyleSheet(f"color: {theme.WARNING};")

        web_port = self.engine.config.web_port
        url = ""
        for iface in list_interfaces(include_virtual=False):
            if not iface.is_ipv6:
                url = f"http://{iface.address}:{web_port}/"
                break
        self.web_url_label.setText(url or "（未找到可用的局域网地址）")
        self.phone_url_label.setText(
            f"手机访问（App 里填这个）：{url}" if url else "手机访问地址：正在检测网络…"
        )
        self.receive_dir_label.setText(self.engine.config.receive_dir)

    def _on_chat_changed(self, event: dict) -> None:
        """A chat message arrived or was sent: refresh the chat tab."""
        view = getattr(self, "chat_view", None)
        if view is None:
            return
        view.reload()
        if event.get("kind") == "chat_message":
            message = event.get("message") or {}
            who = message.get("senderName") or "对方"
            preview = message.get("text") or {
                "image": "[图片]",
                "video": "[视频]",
                "voice": "[语音]",
                "file": "[文件]",
            }.get(str(message.get("kind")), "新消息")
            self.status_left.setText(f"💬 {who}：{preview[:60]}")

    def _on_chat_sent(self, ok: bool, error: str) -> None:
        view = getattr(self, "chat_view", None)
        if view is not None:
            view.send_button.setEnabled(True)
            view.reload()
        if not ok and error:
            self.status_left.setText(f"聊天发送失败：{error}")

    def _remove_device(self, peer: Peer) -> None:
        """Forget a phone (it comes back the next time its page opens)."""
        key = str(peer.info.capabilities.get("clientKey") or "")
        ui = getattr(self, "_web_ui", None)
        if ui is None or not key:
            return
        if ui.remove_client(key):
            self._refresh_devices()
            self.status_left.setText(
                f"已移除「{peer.info.name}」——它下次打开页面会重新出现在这里。"
            )

    def _phone_peers(self) -> list[Peer]:
        """Connected phones, as rows the device list can show.

        A phone speaks HTTP rather than this protocol, so discovery can never
        find it -- but it *is* reachable in the sense the user cares about:
        files can be handed to its page.  Without this row, 「手机连接」 looked
        like it had failed even when the phone was sitting there connected.
        """
        ui = getattr(self, "_web_ui", None)
        if ui is None:
            return []
        try:
            from ..web.server import is_mobile_client

            # Remembered, not "currently online": a phone's page is frozen the
            # moment its screen goes off or the user switches apps, and the
            # user should not have to keep the phone awake (and in the browser)
            # to stay paired.  It stays in the list until it says 断开连接 or
            # the user removes it here.
            clients = [c for c in ui.known_clients() if is_mobile_client(c)]
        except Exception:
            return []
        peers: list[Peer] = []
        for client in clients:
            address = str(client.get("address") or "")
            agent = str(client.get("agent") or "")
            # 安卓 App 和手机浏览器都会出现在这里，但它们是两回事：App 有设备号
            # 和版本，浏览器没有。以前统一写死成"（浏览器）/browser"，于是装了
            # App 的用户看到自己的手机被标成网页版。
            is_app = str(client.get("kind") or "") == "app"
            label = str(client.get("label") or ("安卓 App" if is_app else "手机"))
            peers.append(
                Peer(
                    info=DeviceInfo(
                        device_id=f"web:{address}:{hashlib.sha1(agent.encode('utf-8', 'replace')).hexdigest()[:8]}",
                        name=label if is_app else f"{label}（浏览器）",
                        kind="mobile",
                        platform="android" if is_app else "browser",
                        version=str(client.get("version") or ("app" if is_app else "web")),
                        web_port=self.engine.config.web_port,
                        capabilities={
                            "web": True,
                            "handoff": True,
                            # The device card reads these to say 在线 / 未连接
                            # and "最后在线 x 分钟前".
                            "online": bool(client.get("online")),
                            "secondsAgo": float(client.get("secondsAgo") or 0),
                            "clientKey": str(client.get("key") or ""),
                            "clientKind": "app" if is_app else "browser",
                        },
                    ),
                    address=address,
                    port=self.engine.config.web_port,
                    source="web",
                    trusted=True,  # it is the device the user just paired by QR
                )
            )
        return peers

    def _update_send_button(self) -> None:
        """Say how many devices the next 发送 will hit (multi-select)."""
        try:
            count = len(self.device_table.selected_peers())
        except Exception:
            count = 0
        self.send_button.setText(f"发送到 {count} 台" if count > 1 else "发送")
        self.send_button.setEnabled(count > 0)

    def _refresh_devices(self) -> None:
        try:
            self.device_table.set_peers(self.engine.devices() + self._phone_peers())
        except Exception:
            pass
        self._update_send_button()
        self._refresh_phone_shares()

    def _announce(self) -> None:
        self.engine.announce()
        self.status_left.setText("正在广播自己的存在…")

    def _scan(self) -> None:
        self.engine.scan()
        self._scan_hits = 0
        self.status_left.setText("正在扫描局域网，稍等几秒…")
        # The engine reports completion as an event; this timer is a backstop so
        # the status line can never be left saying "scanning" forever, which is
        # precisely what it used to do.
        self._scan_watchdog = QTimer(self)
        self._scan_watchdog.setSingleShot(True)
        self._scan_watchdog.timeout.connect(
            lambda: self._on_scan_finished(self._scan_hits, "", timed_out=True)
        )
        self._scan_watchdog.start(60_000)

    def _on_scan_finished(self, found: int, error: str = "", timed_out: bool = False) -> None:
        """Report what the scan actually found."""
        watchdog = getattr(self, "_scan_watchdog", None)
        if watchdog is not None:
            watchdog.stop()
        hits = getattr(self, "_scan_hits", 0)
        if error:
            self.status_left.setText(f"扫描出错：{error}")
        elif hits:
            self.status_left.setText(f"扫描完成：发现 {hits} 台设备（已列在左边）")
        elif found:
            # Reachable hosts that did not complete a handshake are not devices;
            # saying "found 3" about them would invent identities.
            self.status_left.setText(
                f"扫描结束：{found} 个地址有响应，但没有一台完成握手（可能是别的程序占着端口）"
            )
        elif timed_out:
            self.status_left.setText("扫描结束：没有发现新设备（看看对方是不是开了防火墙）")
        else:
            self.status_left.setText("扫描完成：没有发现新设备（对方可能开着防火墙，或不在同一网段）")

    def _manual_add(self) -> None:
        from PySide6.QtWidgets import QInputDialog

        text, ok = QInputDialog.getText(
            self, "手动添加设备", "输入对方的 IP 地址（可带端口，如 192.168.1.5:52117）"
        )
        if not ok or not text.strip():
            return
        text = text.strip()
        if ":" in text and not text.startswith("["):
            host, _, port_text = text.rpartition(":")
            try:
                port = int(port_text)
            except ValueError:
                host, port = text, self.engine.port
        else:
            host, port = text, self.engine.port
        self.engine.add_manual_device(host, port)
        self._refresh_devices()

    def _pick_files(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(self, "选择要发送的文件")
        if paths:
            self._add_paths(paths)

    def _pick_directory(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "选择要发送的文件夹")
        if path:
            self._add_paths([path])

    def _add_paths(self, paths: list) -> None:
        added = self.file_list.add_paths(paths)
        if added:
            self.tabs.setCurrentIndex(0)
        self._update_total()

    def _remove_selected(self) -> None:
        self.file_list.remove_selected()
        self._update_total()

    def _clear_files(self) -> None:
        self.file_list.clear_all()
        self._update_total()

    def _update_total(self) -> None:
        paths = self.file_list.paths
        if not paths:
            self.total_label.setText("尚未选择文件")
            return
        self.total_label.setText(
            f"共 {len(paths)} 项，{human_bytes(self.file_list.total_size())}"
        )

    def _send(self) -> None:
        """Send the chosen files to every selected device (multi-select)."""
        peers = self.device_table.selected_peers()
        if not peers:
            QMessageBox.information(self, "还没有选择设备", "请先在左边选择一台接收设备。")
            return
        if not self.file_list.paths:
            QMessageBox.information(self, "还没有选择文件", "请先添加要发送的文件或文件夹。")
            return
        self.tabs.setCurrentIndex(2)
        phone_peers = [p for p in peers if p.source == "web"]
        targets = [p for p in peers if p.source != "web"]
        for peer in phone_peers:
            self._hand_off_to_phone(peer)
        if not targets:
            return
        for peer in targets:
            self._send_to_peer(peer)
        names = "、".join(p.info.name for p in targets)
        self.status_left.setText(
            f"正在发送到 {len(targets)} 台设备（{names}）…"
            + (f"，另有 {len(phone_peers)} 台手机在等它取文件" if phone_peers else "")
        )

    def _send_to_peer(self, peer) -> None:
        """One transfer per target: each gets its own card and its own cancel."""
        pending_key = f"__pending__{time.time()}-{peer.info.device_id[:6]}"
        row = self._add_transfer_row(pending_key, f"发送到 {peer.info.name}", "send")
        row.set_status("正在发送请求…")

        worker = SendWorker(self.engine, peer, list(self.file_list.paths), self.pin_edit.text())

        # SendWorker runs on a plain thread, so it only emits; the queued
        # connection delivers the call on the GUI thread.
        worker.on_done = lambda ok, error, key=pending_key: self.send_finished.emit(ok, error, key)
        self._send_workers.append(worker)
        self._send_workers = [w for w in self._send_workers if w.is_alive() or w is worker]
        worker.start()
        self._transfers[pending_key] = row
        # Only one card can be "the pending row" for adoption; the rest are
        # adopted by transfer id when their sessions start, which is already
        # how a second concurrent send works.
        self._pending_row_key = pending_key

    @Slot(bool, str, str)
    def _on_send_finished(self, ok: bool, error: str, key: str) -> None:
        if getattr(self, "_pending_row_key", None) == key:
            self._pending_row_key = None
        row = self._transfers.get(key)
        if row is not None and not getattr(row, "_finished", False):
            # Keep the status the engine reported (a cancel is not a failure);
            # only fill in a message when there is nothing better to say.
            if ok:
                self._finish_row(key, True, "")
            elif error:
                self._finish_row(key, False, error)
            else:
                self._finish_row(key, False, "对方拒绝或连接中断")
        self._send_workers = [w for w in self._send_workers if w.is_alive()]

    def _hand_off_to_phone(self, peer: Peer) -> None:
        """Give the selected files to a phone's page, for it to pull.

        A browser has no receiving service, so this cannot be a push: the
        desktop publishes the files and the phone downloads them with one tap.
        Saying that plainly is the point -- "已发送" followed by a file that
        never arrives is worse than a sentence about how it actually works.
        """
        ui = getattr(self, "_web_ui", None)
        if ui is None:
            QMessageBox.warning(
                self,
                "浏览器界面没有启动",
                "手机是通过浏览器界面连接的，现在它没有运行，所以没法交给它。",
            )
            return
        entries = ui.share_files(list(self.file_list.paths))
        if not entries:
            QMessageBox.warning(self, "没有可发送的文件", "选中的文件都读不到了。")
            return
        online = bool(peer.info.capabilities.get("online"))
        for entry in entries:
            row = self._add_transfer_row(entry["id"], f"交给 {peer.info.name}：{entry['name']}", "send")
            row.bar.setRange(0, 0)  # 不确定进度：等手机来取
            row.set_status(
                "已放到手机页面，等它在手机上点下载"
                if online
                else "手机现在不在线（熄屏？）——文件已留着，等它回来点下载"
            )
            self._transfers[entry["id"]] = row
            self._phone_shares[entry["id"]] = row
        if online:
            self.status_left.setText(
                f"已把 {len(entries)} 个文件交给手机页面——请在手机上点「下载」。"
            )
        else:
            self.status_left.setText(
                f"已把 {len(entries)} 个文件留给手机（它现在不在线）——手机打开页面就能下载。"
            )

    def _refresh_phone_shares(self) -> None:
        """Move the hand-off rows along as the phone actually takes the files."""
        if not self._phone_shares:
            return
        ui = getattr(self, "_web_ui", None)
        if ui is None:
            return
        try:
            shares = {s["id"]: s for s in ui.shares()}
        except Exception:
            return
        for share_id, row in list(self._phone_shares.items()):
            share = shares.get(share_id)
            if share is None:
                continue
            if share.get("downloaded") and not getattr(row, "_phone_done", False):
                row.bar.setRange(0, 1000)
                row.finish(True, "手机已取走")
                row._phone_done = True  # noqa: SLF001 - our own marker
                self.status_left.setText("手机已经取走了文件。")

    def _adopt_pending_row(self, transfer_id: str, title: str, direction: str) -> TransferRow | None:
        """Reuse the placeholder card for the transfer that just started.

        The card starts under a placeholder key (``__pending__…``) because the
        real transfer id only exists once the session is up.  Renaming it is
        what let 「取消」 point at the right transfer -- but it also meant the
        send worker's ``on_done`` could no longer find the card by its old key,
        so a cancelled send sat at 「正在取消…」 forever while the engine had
        already let it go.  Both keys therefore keep pointing at the same card
        until it finishes.
        """
        key = getattr(self, "_pending_row_key", None)
        row = self._transfers.get(key) if key else None
        if row is None:
            return None
        self._pending_row_key = None
        if key:
            self._row_aliases[transfer_id] = key
        row.transfer_id = transfer_id
        row.title.setText(("⬆  " if direction == "send" else "⬇  ") + title)
        try:
            row.cancel_button.clicked.disconnect()
        except RuntimeError:
            pass
        row.cancel_button.clicked.connect(lambda _c=False, t=transfer_id: self._cancel_transfer(t))
        self._transfers[transfer_id] = row
        return row

    def _finish_row(self, key: str, ok: bool, message: str) -> bool:
        """Finish the card for ``key`` (a transfer id *or* a placeholder key).

        Finishing has to be idempotent and has to work from either name,
        because the two things that end a send -- the ``send_finished`` event
        and the worker thread's callback -- know it by different ones.
        """
        row = self._transfers.get(key)
        if row is None:
            for transfer_id, pending in self._row_aliases.items():
                if transfer_id == key or pending == key:
                    row = self._transfers.get(transfer_id) or self._transfers.get(pending)
                    break
        if row is None:
            return False
        if getattr(row, "_finished", False):
            return True
        # ``TransferRow.finish`` owns the "already finished" flag: marking it
        # here first made the widget skip the very update we came for, so the
        # card kept saying 「正在取消…」 while everything else had moved on.
        row.finish(ok, message)
        # The card can be known by two names; forget both so the empty-state
        # label comes back and no stale key can be finished twice.
        for name, mapped in list(self._transfers.items()):
            if mapped is row:
                self._transfers.pop(name, None)
        for transfer_id, pending in list(self._row_aliases.items()):
            if self._transfers.get(transfer_id) is None and self._transfers.get(pending) is None:
                self._row_aliases.pop(transfer_id, None)
        return True

    def _add_transfer_row(self, transfer_id: str, title: str, direction: str) -> TransferRow:
        row = TransferRow(transfer_id, title, direction, self.transfers_container)
        row.cancel_requested.connect(self._cancel_transfer)
        self.transfers_layout.insertWidget(self.transfers_layout.count() - 1, row)
        self._transfers[transfer_id] = row
        self.transfers_empty.setVisible(False)
        return row

    def _refresh_devices_now(self) -> None:
        """User pressed 刷新: re-read the list and prod the network.

        Both halves matter.  Re-reading shows anything the discovery threads
        already know about, and ``announce_now`` makes every peer answer right
        away instead of at its next 30-second broadcast -- which is the
        difference between "it is not there" and "it is there in half a minute".
        """
        try:
            self.engine.announce()
        except Exception:
            pass
        try:
            self.engine.scan()
        except Exception:
            pass
        self._refresh_devices()
        phones = self._phone_peers()
        self.status_left.setText(
            f"已刷新：{len(self.engine.devices())} 台协议设备、{len(phones)} 台手机（正在重新探测网段）"
        )

    def _cancel_transfer(self, transfer_id: str) -> None:
        accepted = False
        try:
            accepted = bool(self.engine.cancel(transfer_id, "用户取消"))
        except Exception as exc:
            self.status_left.setText(f"取消失败：{exc}")
        if accepted:
            row = self._transfers.get(transfer_id)
            if row is not None:
                row.set_status("正在取消…")
                # A second click would only ask again for something already on
                # its way out; the button comes back with the next transfer.
                row.cancel_button.setEnabled(False)
            return
        # Nothing to cancel: the transfer already ended (or was never active).
        # Saying 「正在取消…」 here is what left the card spinning forever.
        self._finish_row(transfer_id, False, "已取消")

    def _refresh_progress(self) -> None:
        """Poll live transfers and update their rows."""
        try:
            active = self.engine.active_transfers()
        except Exception:
            return
        now = time.monotonic()
        for transfer in active:
            row = self._transfers.get(transfer.transfer_id)
            if row is None:
                label = ("发送到 " if transfer.direction == "send" else "接收自 ") + transfer.peer.name
                row = self._adopt_pending_row(
                    transfer.transfer_id, label, transfer.direction
                ) or self._add_transfer_row(transfer.transfer_id, label, transfer.direction)
                row.set_status("传输中")
            stats = transfer.stats
            done = stats.done_bytes
            row.update_progress(done, max(1, stats.total_bytes), stats.instant_speed_bps, stats.eta_seconds)
        self._last_tick = now

        if not active and not self._transfers:
            self.transfers_empty.setVisible(True)
        elif self.transfers_layout.count() > 1:
            self.transfers_empty.setVisible(False)

    def _add_history(self, name: str, path: str, transfer_id: str) -> None:
        frame = QFrame()
        frame.setObjectName("Card")
        layout = QHBoxLayout(frame)
        layout.setContentsMargins(12, 8, 12, 8)

        label = QLabel(f"📄  {name}")
        label.setToolTip(path)
        layout.addWidget(label, 1)

        size_text = ""
        try:
            size_text = human_bytes(os.path.getsize(path))
        except OSError:
            pass
        size_label = QLabel(size_text)
        size_label.setObjectName("Subtitle")
        layout.addWidget(size_label)

        show = QPushButton("打开所在位置")
        show.clicked.connect(lambda _c=False, p=path: self._reveal(p))
        layout.addWidget(show)

        self.history_layout.insertWidget(0, frame)

    def _reveal(self, path: str) -> None:
        from ..core.platform_open import reveal_in_file_manager

        reveal_in_file_manager(path)

    def _open_receive_dir(self) -> None:
        from ..core.platform_open import open_path

        open_path(self.engine.config.receive_dir)

    def _change_receive_dir(self) -> None:
        path = QFileDialog.getExistingDirectory(
            self, "选择接收文件的保存位置", self.engine.config.receive_dir
        )
        if not path:
            return
        self.engine.config.receive_dir = path
        os.makedirs(path, exist_ok=True)
        self._update_header()
        self._save_settings(silent=True)

    def on_web_ui_started(self, port: int, ui=None) -> None:
        """Record the browser UI's real port and refresh the header."""
        self.engine.config.web_port = port
        self.engine.info.web_port = port
        self._web_ui = ui
        if getattr(self, "chat_view", None) is not None:
            self.chat_view._clients_provider = ui.known_clients  # noqa: SLF001
        self._update_header()
        self._refresh_web_clients()

    def on_web_tls_started(self, port: int) -> None:
        """Remember the HTTPS port: voice messages need that address."""
        self._web_tls_port = int(port)
        self._refresh_web_clients()

    def on_web_ui_failed(self, reason: str) -> None:
        """Surface a browser-UI failure without blocking the desktop app."""
        self.status_left.setText(f"浏览器界面未启动：{reason}（桌面端不受影响）")
        self.web_clients_label.setText("浏览器界面没有启动，手机连不上。")

    def _refresh_web_clients(self) -> None:
        """Say which phones are paired, and whether they are online.

        A phone talks to this app through a web page, not through the peer
        protocol, so it would never appear in the device list on its own -- and
        since its page is frozen whenever the screen is off, "connected" has to
        mean *paired*, not "polling us this second".  Otherwise the only way to
        stay connected would be to keep the phone awake and in the browser.
        """
        ui = getattr(self, "_web_ui", None)
        if ui is None:
            # Before the browser UI is up (or if it failed to start), say that
            # instead of showing an empty row that looks like a rendering bug.
            self.web_clients_label.setText("浏览器界面未启动，手机连不上（见下方提示）。")
            return
        try:
            from ..web.server import is_mobile_client

            phones = [c for c in ui.known_clients() if is_mobile_client(c)]
        except Exception:
            return
        if not phones:
            self.web_clients_label.setText(
                "还没有手机连过。手机扫码打开页面后这里会显示出来；"
                "之后即使熄屏或离开页面，它也会一直记着，直到你在手机上点「断开连接」。"
            )
            return

        def describe(client: dict) -> str:
            return f"{client.get('address', '?')}（{client.get('label', '浏览器')}）"

        online = [c for c in phones if c.get("online")]
        offline = [c for c in phones if not c.get("online")]
        if online:
            text = "✅ 已连接：" + "、".join(describe(c) for c in online[:3])
            if offline:
                text += f"；另有 {len(offline)} 台已配对但不在线（发给它的文件会留着）"
        else:
            text = (
                "已配对但不在线："
                + "、".join(describe(c) for c in offline[:3])
                + "——熄屏或离开页面不影响配对，发给它的文件会留着等它回来。"
            )
        self.web_clients_label.setText(text)

    def _show_qr(self) -> None:
        url = self.web_url_label.text()
        if not url.startswith("http"):
            QMessageBox.information(
                self,
                "没有可用的局域网地址",
                "没有检测到局域网地址，请检查网络连接后重试。",
            )
            return
        # Hand the dialog a way to see connected browsers, so it can say
        # "已连上" the moment the phone opens the page.
        ui = getattr(self, "_web_ui", None)
        providers = ui.clients if ui is not None else None
        alternatives: list[str] = []
        if ui is not None:
            try:
                alternatives = [u for u in ui.urls() if u != url]
            except Exception:
                alternatives = []
        QrDialog(url, self, clients=providers, alternatives=alternatives).exec()

    def _save_settings(self, silent: bool = False) -> None:
        config = self.engine.config
        name = self.name_edit.text().strip()
        if name:
            config.name = name
            self.engine.info.name = name
            from ..core import crypto

            self.engine.identity.name = name
            crypto.save_identity(
                self.engine.identity, os.path.join(config.data_dir, "identity.json")
            )
        config.streams = int(self.streams_spin.value())
        config.pin = self.pin_field.text()
        config.auto_accept_trusted = self.auto_trust_box.isChecked()
        config.auto_accept_all = self.auto_all_box.isChecked()
        config.resume = self.resume_box.isChecked()

        if self.settings_path:
            try:
                payload = {
                    "name": config.name,
                    "receiveDir": config.receive_dir,
                    "streams": config.streams,
                    "pin": config.pin,
                    "autoAcceptTrusted": config.auto_accept_trusted,
                    "autoAcceptAll": config.auto_accept_all,
                    "resume": config.resume,
                    "encrypt": config.encrypt,
                }
                tmp = self.settings_path + ".tmp"
                with open(tmp, "w", encoding="utf-8") as fh:
                    json.dump(payload, fh, indent=2, ensure_ascii=False)
                os.replace(tmp, self.settings_path)
            except OSError as exc:
                if not silent:
                    QMessageBox.warning(self, "保存失败", str(exc))
                return

        self._update_header()
        self.engine.announce()
        if not silent:
            self.status_left.setText("设置已保存并生效")

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt naming
        try:
            self._save_settings(silent=True)
        except Exception:
            pass
        for dialog in list(self._offer_dialogs.values()):
            try:
                dialog.reject()
            except Exception:
                pass
        self.bridge.close()
        super().closeEvent(event)


def load_settings(path: str) -> dict:
    """Read the persisted settings, tolerating a missing or corrupt file."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def apply_settings(config: EngineConfig, settings: dict) -> None:
    """Overlay persisted settings onto an engine configuration."""
    if not settings:
        return
    if settings.get("name"):
        config.name = str(settings["name"])
    if settings.get("receiveDir"):
        config.receive_dir = str(settings["receiveDir"])
    if isinstance(settings.get("streams"), int):
        config.streams = max(1, min(16, int(settings["streams"])))
    if "pin" in settings:
        config.pin = str(settings.get("pin") or "")
    if "autoAcceptTrusted" in settings:
        config.auto_accept_trusted = bool(settings["autoAcceptTrusted"])
    if "autoAcceptAll" in settings:
        config.auto_accept_all = bool(settings["autoAcceptAll"])
    if "resume" in settings:
        config.resume = bool(settings["resume"])
    if "encrypt" in settings:
        config.encrypt = bool(settings["encrypt"])


__all__ = ["MainWindow", "apply_settings", "load_settings"]
