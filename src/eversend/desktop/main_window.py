"""The main application window.

Layout (see ``docs/UI_LAYOUT.md``): a fixed sidebar on the left — 设备操作在顶、
设备列表在中、传输/设置在底 — and one large main area on the right whose top
strip is 对方设备的详细信息, whose middle is the session (会话 / 传输) and whose
bottom is the composer.  The window owns everything that is *not* layout:
discovery, sending, transfer bookkeeping, the browser UI and the settings file.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from dataclasses import replace

from PySide6.QtCore import Qt, QTimer, Signal, Slot
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSpinBox,
    QStackedWidget,
    QStatusBar,
    QVBoxLayout,
    QWidget,
)

from ..core.chat import client_aliases, direct_conversation_id
from ..core.engine import Engine, EngineConfig
from ..core.model import DeviceInfo, Peer, human_bytes
from ..core.sockutil import list_interfaces
from . import theme
from .bridge import EngineBridge, SendWorker
from .chat_view import ChatView, guess_kind
from .session_view import SessionView
from .sidebar import Sidebar
from .transfers_view import TransferRow, TransfersView
from .widgets import OfferDialog, QrDialog, repaint_icons


#: 「以前见过」的设备记多少台、记多久。引擎的发现表有 90 秒 TTL，一台电脑关掉
#: 一分半就从列表里消失了 —— 用户要的「离线」那一栏就没东西可放。身份快照落盘
#: （``data_dir/known_devices.json``），重启之后离线那一段也还在。
KNOWN_DEVICE_LIMIT = 24
KNOWN_DEVICE_TTL = 24 * 3600.0


def peer_is_sendable(peer: Peer) -> bool:
    """这台设备现在能不能真的收文件。

    「记录里的会话」和「以前见过、现在不在线」两种行都进不了发送队列：前者没有
    地址，后者地址还在但人已经走了 —— 一条发不出去却显示"正在发送"的传输比一句
    "它不在线"更糟。手机（网页客户端）另有交接路径，不在这里判。
    """
    return peer.source not in ("history", "offline")


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
        #: 手机分享号 -> (原始文件路径, peer)。手机那条路"重试"要按原样再发布一次。
        self._phone_share_paths: dict[str, tuple[str, Peer]] = {}
        #: 分享条目上一次看到的已发字节数 + 时刻，用来识别"手机取到一半停住了"。
        self._phone_share_watch: dict[str, tuple[int, float]] = {}
        #: 文件消息 id -> 中文失败原因。store 里没有 reason 这一列，而"手机掉线 /
        #: 文件读不到 / 分享被清掉"这些原因只有窗口知道（会见 ChatView._failed_text）。
        self._transfer_failures: dict[str, str] = {}
        #: 侧栏里每一行要用的会话状态（未读/置顶/最后一条），由 ChatView 广播。
        self._conversations: list[dict] = []
        #: transfer id -> 发送方名字，收到的文件在「接收」表里要写出"来自谁"。
        self._recv_from: dict[str, str] = {}
        #: transfer id -> 结束时刻（见 :meth:`_finish_row`）。
        self._finished_ids: dict[str, float] = {}
        #: 已经进过「接收」历史表的传输号：传输结束时把"正在传"那一行收起来，
        #: 一个文件只留一行（用户明确要求：同一个文件的操作和状态合并成一行）。
        self._history_transfers: set[str] = set()
        #: **用户主动取消过**的传输号（含占位名）。取消是"点了按钮"这件事本身
        #: 表达的，不能靠去猜结束回调里的文案 —— 发送线程那条路回来的是空串，
        #: 猜字串的结果就是用户自己点的取消被显示成红色「失败」。
        self._cancelled_ids: set[str] = set()
        #: 传输号 -> 会话里那几条记录的消息 id（结束时改状态用）。
        self._transfer_messages: dict[str, list[str]] = {}
        #: 传输号 -> (对端设备号, 设备名)。
        self._transfer_devices: dict[str, tuple[str, str]] = {}
        #: 第一次刷新设备之后自动打开一个会话（和以前打开上一次的会话一样）。
        self._restored_session = False
        #: device_id -> 身份快照（「以前见过」的设备，落盘在 known_devices.json）。
        self._known_devices: dict[str, dict] = {}
        self._known_dirty = False

        self.setWindowTitle("韧传 EverSend")
        self.resize(1180, 780)
        self.setMinimumSize(900, 620)

        #: Set by :meth:`on_web_ui_started`; ``None`` until (and unless) the
        #: browser interface is up.
        self._web_ui = None
        #: HTTPS port, when the secure copy of the page started (0 = none).
        self._web_tls_port = 0
        #: Devices seen since the current scan started, and the backstop timer.
        self._scan_hits = 0
        self._scan_watchdog: QTimer | None = None

        self._apply_saved_scale()
        self._build_ui()
        self._wire_open_failures()
        self._load_known_devices()
        self._apply_saved_retention()
        # 自动整理必须自己排一次，别等"会话列表变化"来触发：ChatView 在构造时就
        # reload 过一次，而那时 bridge 还没接上（信号在那一次广播里丢了），于是
        # 安静的启动里根本没人调用 _fold_conversations_soon —— 审计员实测 20 秒
        # 空闲里 _on_conversations 调用 0 次、两段会话一直没并。以前它"碰巧会跑"
        # 是因为自动打开会话会 mark_read、把签名改掉、下一次 tick 再广播一次。
        self._fold_conversations_soon()
        self._connect_bridge()

        self._tick = QTimer(self)
        self._tick.setInterval(400)
        self._tick.timeout.connect(self._refresh_progress)
        self._tick.start()

        self._discovery_tick = QTimer(self)
        self._discovery_tick.setInterval(2000)
        self._discovery_tick.timeout.connect(self._refresh_devices)
        self._discovery_tick.start()

        # 引擎比窗口先起来时收不到 engine_started（见 _announce_ready），
        # 所以再兜一次底：起来之后就把「正在启动…」换掉。
        QTimer.singleShot(400, self._announce_ready)
        QTimer.singleShot(1500, self._announce_ready)

    # ------------------------------------------------------------------
    # construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        root = QHBoxLayout(central)
        # 侧栏与主区之间只有一条发丝线（在侧栏自己的样式里），所以这里不留边距。
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        root.addWidget(self._build_sidebar())

        # ---- 主区：会话（顶部对方设备详情 + 中间会话/传输 + 底部输入区）----
        self.chat_view = ChatView(self.engine)
        self.chat_view.send_finished.connect(self._on_chat_sent)
        self.chat_view.send_requested.connect(self._compose_send)
        self.chat_view.attach_requested.connect(self._pick_files)
        self.chat_view.paths_dropped.connect(self._add_paths)
        self.chat_view.conversations_changed.connect(self._on_conversations)
        self.chat_view.conversation_changed.connect(self._on_conversation_changed)
        self.chat_view.preview_requested.connect(self.preview_path)
        # 文件传输失败的原因写进气泡（"手机掉线 / 文件读不到 / 分享被清掉"这些
        # 只有窗口知道，chat store 里没有 reason 这一列）。
        self.chat_view.set_failure_reason_provider(self._transfer_failure_reason)

        self.transfers_view = TransfersView()
        self.transfers_view.open_folder_clicked.connect(self._open_receive_dir)
        self.transfers_view.clear_clicked.connect(self._clear_transfers)
        self.transfers_view.cancel_requested.connect(self._cancel_transfer)
        self.transfers_view.retry_requested.connect(self._retry_transfer)
        self.transfers_view.preview_requested.connect(self.preview_path)
        self.transfers_view.compose_requested.connect(self._go_compose)
        self.transfers_view.back_requested.connect(
            lambda: self.show_page("session", view="chat")
        )
        # 「重新发送」要从文件动作矩阵里真的发得出去（见 desktop-chat 的约定）。
        self.transfers_view.recv_table.resend_handler = self._resend_file
        # 接收记录要能跨重启（G-12）：会话库里有每个收到文件的记录，启动时把
        # 文件还在的那些恢复成历史行；以前传输页重启后永远是空的。
        self._restore_received_history()

        self.session_view = SessionView(self.chat_view, self.transfers_view)
        self.session_view.paths_dropped.connect(self._add_paths)
        # 宪章 §3.1：会话页的「清空会话」收进顶部 ``⋯``；传输页的那几个动作在
        # 传输页自己的工具条上（``打开接收文件夹`` + ``⋯``），这里不重复摆。
        self.session_view.header.clear_clicked.connect(self._clear_current)
        # 分段控件已经删掉（页面入口只有侧栏底部导航），这个信号只作兼容保留。
        self.session_view.header.view_changed.connect(
            lambda key: self.sidebar.set_active_page(
                "transfers" if key == "transfers" else "chat"
            )
        )

        self.settings_page = self._build_settings_page()

        self.main_stack = QStackedWidget()
        self.main_stack.addWidget(self.session_view)
        self.main_stack.addWidget(self.settings_page)
        root.addWidget(self.main_stack, 1)

        # -- 兼容旧名字：窗口之外（测试、脚本）一直按这些名字取控件 ----------
        self.file_list = self.chat_view.attachment_bar
        self.file_list.changed.connect(self._update_total)
        self.send_button = self.chat_view.send_button
        self.pin_edit = self.chat_view.pin_edit
        self.total_label = self.chat_view.total_label

        status = QStatusBar()
        self.setStatusBar(status)
        self.status_left = QLabel("正在启动…")
        self.status_right = QLabel("")
        status.addWidget(self.status_left, 1)
        status.addPermanentWidget(self.status_right)

    def _build_sidebar(self) -> Sidebar:
        self.sidebar = Sidebar()
        #: 设备列表分两段（在线 / 离线）；``device_table`` 是在线那一张，
        #: 老代码和测试一直按这个名字取设备，所以它保持不变。
        self.device_table = self.sidebar.device_table
        self.offline_table = self.sidebar.offline_table
        self.device_table.itemSelectionChanged.connect(self._update_send_button)

        # 宪章 §3.1：「⟳ 刷新」和「◎ 扫描局域网」合并成一个「扫描」——先催一次
        # 公告（快），没人应答时 :meth:`_scan` 会自己升级成主动扫描（慢但管用）。
        self.sidebar.scan_clicked.connect(self._refresh_devices_now)
        self.sidebar.manual_clicked.connect(self._manual_add)
        self.sidebar.qr_clicked.connect(self._show_qr)
        self.sidebar.new_group_clicked.connect(lambda: self.chat_view.new_group())
        self.sidebar.peer_activated.connect(self._open_peer_session)
        self.sidebar.group_activated.connect(self._open_group_session)
        self.sidebar.address_clicked.connect(
            lambda text: self.status_left.setText(f"已复制本机地址：{text.split('  ·  ')[0].strip()}")
        )
        self.sidebar.transfers_clicked.connect(
            lambda: self.show_page("session", view="transfers")
        )
        self.sidebar.settings_clicked.connect(lambda: self.show_page("settings"))
        self.sidebar.context_requested.connect(self._device_context_menu)
        self.sidebar.section_action.connect(self._on_section_action)
        return self.sidebar

    def _on_section_action(self, action: str) -> None:
        """段标题右键的动作（目前只有「清空离线设备列表」）。"""
        if action == "clear-offline":
            self.clear_offline_devices()

    def _apply_saved_scale(self) -> None:
        """套用设置文件里的字号档，并把字体真的换掉。

        ``app.py`` 在 ``QApplication`` 建好之前就会调 ``theme.set_scale``（见
        :func:`apply_settings`），所以正常启动时这里是幂等的；测试或嵌入式用法
        直接 new 一个 MainWindow 时，这一步保证字号档也生效。
        """
        settings = load_settings(self.settings_path) if self.settings_path else {}
        choice = str(settings.get("font_scale") or "").strip()
        if choice:
            theme.set_scale(choice)
        self._restyle_app()

    def _restyle_app(self) -> None:
        """重新套用字体与样式表（主题或字号变化后调用）。

        Qt 不会自己重算：``application_font`` 是快照，样式表里的 font-size 也要
        重新生成一遍；自己画的图标也得重画（它们是按当前配色/尺寸画的位图）。
        """
        app = QApplication.instance()
        if app is not None:
            app.setFont(theme.application_font())
            app.setStyleSheet(theme.stylesheet(theme.is_dark()))
            app.setWindowIcon(theme.app_icon(128, theme.is_dark()))
        repaint_icons(self)
        for widget in self.findChildren(QWidget):
            widget.update()

    def _apply_saved_retention(self) -> None:
        """启动时套用「记录保留策略」（宪章 C7：默认留最近 500 条 / 30 天）。

        引擎建好之后才能设：``prune_records`` 改的是聊天库自己的参数。
        """
        settings = load_settings(self.settings_path) if self.settings_path else {}
        try:
            if isinstance(settings.get("keepRecords"), int):
                self.engine.chat.keep_records = max(1, int(settings["keepRecords"]))
            if isinstance(settings.get("keepDays"), int):
                self.engine.chat.keep_days = max(1, int(settings["keepDays"]))
            self.engine.chat.prune_records()
        except Exception:
            pass

    # ------------------------------------------------------------------
    # 设置页：分组卡片（宪章 §3.4）
    # ------------------------------------------------------------------

    def _settings_card(self, layout: QVBoxLayout, title: str, hint: str = "") -> QVBoxLayout:
        """一张分组卡片：标题 + 说明 + 控件（宪章 §3.4 每组都长这样）。"""
        card = QFrame()
        card.setObjectName("Card")
        box = QVBoxLayout(card)
        box.setContentsMargins(
            theme.SPACE["lg"], theme.SPACE["md"], theme.SPACE["lg"], theme.SPACE["lg"]
        )
        box.setSpacing(theme.SPACE["sm"])
        heading = QLabel(title)
        heading.setObjectName("SectionTitle")
        box.addWidget(heading)
        if hint:
            note = QLabel(hint)
            note.setObjectName("Subtitle")
            note.setWordWrap(True)
            box.addWidget(note)
        layout.addWidget(card)
        return box

    def _settings_form(self, box: QVBoxLayout) -> QFormLayout:
        form = QFormLayout()
        form.setContentsMargins(0, 0, 0, 0)
        form.setSpacing(theme.SPACE["sm"])
        form.setLabelAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        box.addLayout(form)
        return form

    def _build_settings_page(self) -> QWidget:
        """设置：点侧栏底部的「设置」把主区换成这一页（侧栏不动）。

        宪章 §3.4：分组卡片。参数按"改完要保存"和"立刻生效"两类摆在明面上，
        最下面那张卡不会再被窗口底边裁掉（内容区可滚，底部留 24px）。
        """
        page = QWidget()
        outer = QVBoxLayout(page)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        header = QFrame()
        header.setObjectName("PageHeader")
        head_row = QHBoxLayout(header)
        head_row.setContentsMargins(
            theme.SPACE["lg"], theme.SPACE["md"], theme.SPACE["lg"], theme.SPACE["md"]
        )
        head_row.setSpacing(theme.SPACE["sm"])
        back = QPushButton("  ←  返回会话")
        back.setObjectName("Ghost")
        back.setToolTip("回到设备会话")
        back.clicked.connect(lambda: self.show_page("session"))
        head_row.addWidget(back)
        title = QLabel("设置")
        title.setObjectName("PeerName")
        head_row.addWidget(title)
        head_row.addStretch(1)
        self.save_button = QPushButton("保存设置")
        self.save_button.setObjectName("Primary")
        self.save_button.clicked.connect(self._save_settings)
        head_row.addWidget(self.save_button)
        outer.addWidget(header)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        inner = QWidget()
        layout = QVBoxLayout(inner)
        # 底部留一格 2xl：最后一张卡下面要有留白，不然滚到底也是"贴着边"。
        layout.setContentsMargins(
            theme.SPACE["lg"], theme.SPACE["lg"], theme.SPACE["lg"], theme.SPACE["2xl"]
        )
        layout.setSpacing(theme.SPACE["lg"])

        # -- ① 本机 ---------------------------------------------------------
        box = self._settings_card(
            layout, "本机", "这三项决定了别人在列表里看到你时的样子。"
        )
        form = self._settings_form(box)
        self.name_edit = QLineEdit(self.engine.config.name)
        self.name_edit.setMaxLength(48)
        form.addRow(form_label("设备名称"), self.name_edit)
        self.device_id_label = QLabel(self.engine.identity.device_id)
        self.device_id_label.setObjectName("Mono")
        self.device_id_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        form.addRow(form_label("设备 ID"), self.device_id_label)
        self.port_label = QLabel(str(self.engine.port or "（启动中）"))
        form.addRow(form_label("传输端口"), self.port_label)
        self.encryption_label = QLabel("")
        form.addRow(form_label("传输加密"), self.encryption_label)

        # -- ② 外观 ---------------------------------------------------------
        box = self._settings_card(
            layout, "外观", "深色是默认主题，和网页版、手机 App 是同一套配色。"
        )
        form = self._settings_form(box)
        self.theme_combo = QComboBox()
        self.theme_combo.addItem("深色（默认）", "dark")
        self.theme_combo.addItem("浅色", "light")
        self.theme_combo.setCurrentIndex(0 if theme.is_dark() else 1)
        self.theme_combo.currentIndexChanged.connect(self._apply_theme_choice)
        form.addRow(form_label("主题"), self.theme_combo)

        # 字号档（宪章 §3.4）：只缩放字号，不缩间距与控件高度 —— 那是"密度"。
        self.font_combo = QComboBox()
        for key, label in theme.SCALE_LABELS.items():
            self.font_combo.addItem(label, key)
        current = theme.current_scale_name()
        index = self.font_combo.findData(current if current != "custom" else "standard")
        self.font_combo.setCurrentIndex(max(0, index))
        self.font_combo.setToolTip("紧凑多塞几行、宽松看着省力；改完立刻生效并记进设置。")
        self.font_combo.currentIndexChanged.connect(self._apply_font_choice)
        form.addRow(form_label("字号"), self.font_combo)

        # -- ③ 传输 ---------------------------------------------------------
        box = self._settings_card(layout, "传输", "接收位置、并发数、以及「要不要先问我」。")
        form = self._settings_form(box)
        self.streams_spin = QSpinBox()
        self.streams_spin.setRange(1, 16)
        self.streams_spin.setValue(self.engine.config.streams)
        self.streams_spin.setToolTip(
            "并行连接数。千兆网卡 4 条足够；2.5G/万兆或高延迟链路可以调到 8。\n"
            "机械硬盘上会自动降到 2 条，因为随机写入反而更慢。"
        )
        form.addRow(form_label("并行连接数"), self.streams_spin)

        self.pin_field = QLineEdit(self.engine.config.pin)
        self.pin_field.setPlaceholderText("留空表示不需要 PIN")
        self.pin_field.setMaxLength(32)
        self.pin_field.setToolTip("设置后，发送方必须输入相同的 PIN 才能传输。")
        form.addRow(form_label("接收 PIN"), self.pin_field)

        self.receive_dir_label = QLabel(self.engine.config.receive_dir)
        self.receive_dir_label.setObjectName("Mono")
        self.receive_dir_label.setWordWrap(True)
        self.receive_dir_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        form.addRow(form_label("接收位置"), self.receive_dir_label)
        # 「更改接收位置」的正主在这里（它是配置）；传输页只留「打开接收文件夹」。
        self.change_dir_button = QPushButton("更改接收位置…")
        self.change_dir_button.clicked.connect(self._change_receive_dir)
        change_row = QHBoxLayout()
        change_row.setContentsMargins(0, 0, 0, 0)
        change_row.addWidget(self.change_dir_button)
        change_row.addStretch(1)
        form.addRow("", change_row)

        # 「自动接收」是**一个**三选一，不是两个能同时勾上的复选：
        # "接收所有"已经把"接收已信任"包含进去了，两个都打勾只会让人以为
        # 有两个独立开关（Lead 在真机截图上点出来的）。
        self.auto_group = QButtonGroup(self)
        self.auto_radio_off = QRadioButton("每次都先问我（不自动接收）")
        self.auto_radio_trusted = QRadioButton("只自动接收已信任的设备")
        self.auto_radio_all = QRadioButton("自动接收所有设备（不安全）")
        for index, radio in enumerate(
            (self.auto_radio_off, self.auto_radio_trusted, self.auto_radio_all)
        ):
            self.auto_group.addButton(radio, index)
            box.addWidget(radio)
        if self.engine.config.auto_accept_all:
            self.auto_radio_all.setChecked(True)
        elif self.engine.config.auto_accept_trusted:
            self.auto_radio_trusted.setChecked(True)
        else:
            self.auto_radio_off.setChecked(True)
        self.auto_radio_all.setToolTip(
            "任何设备发来的文件都不问自取，只在完全可信的网络里用。"
        )
        self.auto_radio_trusted.setToolTip("配对过的设备直接收，其它设备仍然先问我。")
        self.resume_box = QCheckBox("支持断点续传")
        self.resume_box.setChecked(self.engine.config.resume)
        self.resume_box.setToolTip("关闭后每次重新传输都从头开始，一般不需要关闭。")
        box.addWidget(self.resume_box)
        self.encrypt_box = QCheckBox("加密传输内容")
        self.encrypt_box.setChecked(self.engine.config.encrypt)
        self.encrypt_box.setEnabled(False)
        self.encrypt_box.setToolTip("加密设置在启动时确定，修改后需要重启。")
        box.addWidget(self.encrypt_box)

        # -- ④ 网络与设备 ---------------------------------------------------
        box = self._settings_card(
            layout, "网络与设备", "发现通道哪个开着、以前见过哪些设备，都在这里。"
        )
        form = self._settings_form(box)
        self.multicast_box = QCheckBox("组播发现（同一网段最快的通道）")
        self.multicast_box.setChecked(True)
        self.multicast_box.setEnabled(False)
        self.multicast_box.setToolTip("组播由引擎在启动时打开；关掉它就没有「秒级出现」了。")
        box.addWidget(self.multicast_box)
        self.broadcast_box = QCheckBox("广播发现（组播被路由器拦掉时的后备）")
        self.broadcast_box.setChecked(bool(getattr(self.engine.config, "enable_broadcast", True)))
        box.addWidget(self.broadcast_box)
        self.mdns_box = QCheckBox("mDNS 发现（装了 Bonjour/Avahi 的机器）")
        self.mdns_box.setChecked(bool(getattr(self.engine.config, "enable_mdns", True)))
        box.addWidget(self.mdns_box)
        for widget, field in (
            (self.broadcast_box, "enable_broadcast"),
            (self.mdns_box, "enable_mdns"),
        ):
            widget.toggled.connect(
                lambda checked, name=field: self._set_discovery_channel(name, checked)
            )

        self.offline_count_label = QLabel("")
        self.offline_count_label.setObjectName("Subtitle")
        self.offline_count_label.setWordWrap(True)
        box.addWidget(self.offline_count_label)
        care_row = QHBoxLayout()
        care_row.setSpacing(theme.SPACE["sm"])
        self.clear_offline_button = QPushButton("清空离线设备…")
        self.clear_offline_button.setToolTip(
            "把「以前见过、现在不在线」的设备从列表里去掉（聊天记录保留）"
        )
        self.clear_offline_button.clicked.connect(self.clear_offline_devices)
        care_row.addWidget(self.clear_offline_button)
        self.fold_button = QPushButton("整理重复会话…")
        self.fold_button.setToolTip("同一台设备因为换地址/换写法散成几段会话，并回一处")
        self.fold_button.clicked.connect(lambda: self.fold_conversations(strong=True, ask=True))
        care_row.addWidget(self.fold_button)
        care_row.addStretch(1)
        box.addLayout(care_row)

        self.web_url_label = QLabel("")
        self.web_url_label.setObjectName("Mono")
        self.web_url_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.web_url_label.setWordWrap(True)
        form.addRow(form_label("手机访问地址"), self.web_url_label)
        self.web_clients_label = QLabel("")
        self.web_clients_label.setObjectName("Subtitle")
        self.web_clients_label.setWordWrap(True)
        form.addRow(form_label("已连接的手机"), self.web_clients_label)

        # -- ⑤ 数据与维护 ---------------------------------------------------
        box = self._settings_card(
            layout, "数据与维护", "记录留多久、留多少条，以及本机数据放在哪。"
        )
        form = self._settings_form(box)
        self.keep_count_spin = QSpinBox()
        self.keep_count_spin.setRange(50, 20000)
        self.keep_count_spin.setSingleStep(50)
        self.keep_count_spin.setValue(int(getattr(self.engine.chat, "keep_records", 500)))
        self.keep_count_spin.setToolTip("传输记录最多留多少条，超出的旧记录会被清掉。")
        form.addRow(form_label("保留条数"), self.keep_count_spin)
        self.keep_days_spin = QSpinBox()
        self.keep_days_spin.setRange(1, 3650)
        self.keep_days_spin.setValue(int(getattr(self.engine.chat, "keep_days", 30)))
        self.keep_days_spin.setToolTip("超过这么多天的传输记录会被清掉。")
        form.addRow(form_label("保留天数"), self.keep_days_spin)
        maintenance = QHBoxLayout()
        maintenance.setSpacing(theme.SPACE["sm"])
        self.clear_records_button = QPushButton("清空传输记录…")
        self.clear_records_button.setToolTip("只清空传输记录表，聊天消息不动")
        self.clear_records_button.clicked.connect(self._clear_transfers)
        maintenance.addWidget(self.clear_records_button)
        self.open_data_button = QPushButton("打开数据目录")
        self.open_data_button.clicked.connect(self._open_data_dir)
        maintenance.addWidget(self.open_data_button)
        maintenance.addStretch(1)
        box.addLayout(maintenance)

        # -- ⑥ 诊断（E2）----------------------------------------------------
        box = self._settings_card(
            layout, "诊断", "看不到对方时，把这张卡截图发出来就能定位。"
        )
        self.diagnosis_label = QLabel("")
        self.diagnosis_label.setObjectName("Subtitle")
        self.diagnosis_label.setWordWrap(True)
        self.diagnosis_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        box.addWidget(self.diagnosis_label)
        diagnose_row = QHBoxLayout()
        diagnose_row.setSpacing(theme.SPACE["sm"])
        self.refresh_diagnosis_button = QPushButton("重新检测")
        self.refresh_diagnosis_button.clicked.connect(self._refresh_diagnosis)
        diagnose_row.addWidget(self.refresh_diagnosis_button)
        self.export_diagnosis_button = QPushButton("导出诊断信息…")
        self.export_diagnosis_button.clicked.connect(self._export_diagnosis)
        diagnose_row.addWidget(self.export_diagnosis_button)
        diagnose_row.addStretch(1)
        box.addLayout(diagnose_row)

        # -- ⑦ 关于 ---------------------------------------------------------
        box = self._settings_card(layout, "关于", "")
        self.about_label = QLabel("")
        self.about_label.setObjectName("Subtitle")
        self.about_label.setWordWrap(True)
        self.about_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        box.addWidget(self.about_label)

        layout.addStretch(1)
        scroll.setWidget(inner)
        #: 让"设置页滚到底"这件事有个名字（截图脚本与测试都用它）。
        self.settings_scroll = scroll
        outer.addWidget(scroll, 1)
        self._refresh_diagnosis()
        self._refresh_about()
        return page

    # -- 设置页的即时动作 ---------------------------------------------------

    def _set_discovery_channel(self, field: str, enabled: bool) -> None:
        """发现通道开关：立刻写回引擎配置并重新公告一次。"""
        try:
            setattr(self.engine.config, field, bool(enabled))
        except Exception:
            return
        try:
            self.engine.announce()
        except Exception:
            pass
        label = "广播发现" if field == "enable_broadcast" else "mDNS 发现"
        self.status_left.setText(f"{label}已{'打开' if enabled else '关闭'}（重启后仍然生效）。")

    def _open_data_dir(self) -> None:
        from ..core.platform_open import open_path

        open_path(self.engine.config.data_dir)

    def _refresh_about(self) -> None:
        from ..core.constants import APP_VERSION

        self.about_label.setText(
            f"韧传 EverSend {APP_VERSION} · 局域网直传（HTTP/SSE + 自定义发现协议）\n"
            "开源许可：见仓库根目录的 LICENSE；二维码生成用的是项目自带的实现。"
        )

    def _refresh_diagnosis(self) -> None:
        """诊断卡片（宪章 E2）：本机地址、端口、三个发现通道的统计、最近错误。

        引擎提供 ``diagnostics_snapshot()`` 就用它；万一还没有（老引擎 / 单测
        里换了个假引擎），退回到"我们现在知道的"，而不是让整页崩掉。
        """
        lines: list[str] = []
        address = self.web_url_label.text() or "（未找到局域网地址）"
        lines.append(f"手机访问地址：{address}")
        lines.append(f"监听端口：{self.engine.port or '（启动中）'}")
        snapshot: dict = {}
        # 网络层给的 engine.diagnostics() 是完整的那一份；老引擎 / 单测里没有它时
        # 退回 discovery 自己的快照。
        provider = getattr(self.engine, "diagnostics", None)
        if callable(provider):
            try:
                snapshot = dict(provider())
            except Exception:
                snapshot = {}
        if not snapshot:
            discovery = getattr(self.engine, "discovery", None)
            fallback = getattr(discovery, "diagnostics_snapshot", None)
            if callable(fallback):
                try:
                    snapshot = dict(fallback())
                except Exception:
                    snapshot = {}
        channels = snapshot.get("channels") or {}
        if channels:
            parts = []
            for name, label in (
                ("multicast", "组播"),
                ("broadcast", "广播"),
                ("mdns", "mDNS"),
                ("scan", "主动扫描"),
            ):
                stats = channels.get(name) or {}
                if not stats:
                    continue
                enabled = "开" if stats.get("enabled", True) else "关"
                success = stats.get("successes", stats.get("success", stats.get("hits", 0)))
                attempts = stats.get("attempts", stats.get("sent", 0))
                last_error = str(stats.get("lastErrorText") or stats.get("lastError") or "")
                detail = f"{label}：{enabled}，成功 {success}/{attempts}"
                if last_error:
                    detail += f"（最近一次出错：{last_error}）"
                parts.append(detail)
            if parts:
                lines.append("发现通道 —— " + "；".join(parts))
            first = snapshot.get("firstAnnounceMs")
            if first is not None:
                lines.append(f"启动后首次公告：{first} 毫秒（目标 ≤300ms）")
        peers = snapshot.get("peers")
        if peers is not None:
            lines.append(f"当前能看见的设备：{peers} 台；记得住的：{snapshot.get('known', '?')} 台")
        suggestions = snapshot.get("suggestions") or []
        if suggestions:
            lines.append("看不到对方时的排查：")
            for item in suggestions[:4]:
                if isinstance(item, dict):
                    title = str(item.get("title") or "").strip()
                    detail = str(item.get("detail") or item.get("text") or "").strip()
                    line = f"{title}（{detail}）" if title and detail else (title or detail)
                else:
                    line = str(item)
                lines.append(f"  · {line}")
        errors = snapshot.get("recentErrors") or snapshot.get("scanErrors") or []
        notes = list(errors) + list(snapshot.get("notes") or [])
        if notes:
            lines.append("最近错误：")
            for note in notes[-5:]:
                # 网络层现在给的是 dict（{ts, level, message, text}），老的是字符串。
                if isinstance(note, dict):
                    text = str(note.get("text") or note.get("message") or note.get("kind") or "")
                    if note.get("ts"):
                        try:
                            text = time.strftime("%H:%M:%S", time.localtime(float(note["ts"]))) + "  " + text
                        except (TypeError, ValueError, OSError):
                            pass
                else:
                    text = str(note)
                lines.append(f"  · {text}")
        if not channels and not suggestions:
            lines.append(f"设备表：{len(self.engine.devices())} 台（引擎未提供详细的通道统计）")
        self.diagnosis_label.setText("\n".join(lines))

    def _export_diagnosis(self) -> None:
        """导出诊断信息：一个纯文本文件，出问题可以直接发给别人看。"""
        default = os.path.join(
            self.engine.config.data_dir, time.strftime("诊断-%Y%m%d-%H%M%S.txt")
        )
        path, _filter = QFileDialog.getSaveFileName(self, "导出诊断信息", default, "文本文件 (*.txt)")
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(self.diagnosis_label.text() + "\n\n")
                exporter = getattr(self.engine, "export_diagnostics", None)
                snapshot_text = ""
                if callable(exporter):
                    snapshot_text = str(exporter() or "")
                if not snapshot_text:
                    provider = getattr(self.engine, "diagnostics", None)
                    if callable(provider):
                        snapshot_text = json.dumps(
                            provider(), ensure_ascii=False, indent=2, default=str
                        )
                handle.write(snapshot_text or "（引擎没有提供更详细的快照）")
        except OSError as exc:
            QMessageBox.warning(self, "导出失败", str(exc))
            return
        self.status_left.setText(f"诊断信息已导出到 {path}")

    def show_page(self, page: str = "session", view: str = "") -> None:
        """主区切到 ``session``（可指定 ``chat`` / ``transfers``）或 ``settings``。

        这是**唯一**的页面切换入口：侧栏底部那两个导航项、设备行点击、
        传输页的「← 返回会话」都走这里（宪章 §3.1：主区顶部的分段控件已删）。
        """
        if page == "settings":
            # 设置页上写着"离线那栏有几台"、诊断卡也要现算，进去之前先刷新。
            self._refresh_offline_count()
            self._refresh_diagnosis()
            self.main_stack.setCurrentWidget(self.settings_page)
            self.sidebar.set_active_page("settings")
            return
        self.main_stack.setCurrentWidget(self.session_view)
        if view:
            self.session_view.show_view(view)
        else:
            self.session_view.header.set_view(self.session_view.current_view())
        self.sidebar.set_active_page(
            "transfers" if self.session_view.current_view() == "transfers" else "chat"
        )

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
        self.bridge.local_upload.connect(self._on_local_upload)
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
        transfer_id = str(event.get("transfer_id", ""))
        info = event.get("peer")
        if transfer_id and info is not None and getattr(info, "device_id", ""):
            self._transfer_devices[transfer_id] = (
                str(info.device_id),
                str(getattr(info, "name", "") or ""),
            )
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
        dialog.activateWindow()
        # 弹窗被主窗口挡住 = 对方那边一直"正在发送"、这边什么都没有。除了抬到最前，
        # 再在状态栏留一句话并让任务栏闪一下 —— 用户至少有个地方能看出"在等我点"。
        who = str((event.get("peer") or {}).get("name") or "有人")
        files = event.get("files") or []
        self.status_left.setText(
            f"{who} 想发 {len(files)} 个文件给你 —— 在弹出的窗口里点「接收」。"
        )
        QApplication.alert(self, 0)

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
        # 网络层现在同时给 ``error``（英文技术串）和 ``error_text``（中文原因），
        # 界面只显示后者 —— 用户不该看到 "connection refused"（宪章 E3/C14）。
        error = str(event.get("error_text") or event.get("error") or "")
        code = str(event.get("error_code") or "")
        ok = status in ("done", "finished")
        message = error or ("完成" if ok else ("已取消" if status == "cancelled" else "已失败"))
        self._finish_row(transfer_id, ok, message)
        self._finish_transfer_messages(transfer_id, ok)
        if ok:
            self.status_left.setText(
                f"传输完成：{human_bytes(int(event.get('bytes', 0)))}"
            )
        else:
            reason = error or {
                "cancelled": "你取消了这次传输",
                "rejected": "对方拒绝了这次传输",
                "timeout": "传输超时（对方可能不在线了）",
                "connection_refused": "连接被拒（对方没在监听，或防火墙拦了）",
                "disk_full": "接收方磁盘空间不足",
                "checksum_failed": "校验失败，文件可能在路上损坏了",
            }.get(code, "原因不明")
            self.status_left.setText(f"传输失败：{reason}" + (f"（{code}）" if code else ""))

    @Slot(str, int, str, str)
    def _on_file_done(self, transfer_id: str, _index: int, name: str, path: str) -> None:
        self._add_history(name, path, transfer_id)

    @Slot(dict)
    def _on_local_upload(self, event: dict) -> None:
        """手机把文件"发给这台电脑"：文件已落盘，这里把它记进接收表与会话。

        这条路上没有协议传输（手机是纯 HTTP 客户端，它只会 POST 上来），所以既没有
        ``transfer_id``、也没有 ``file_done``。以前电脑端什么都不记 —— 文件在磁盘
        上，用户在界面里翻不到"谁给我发过什么"（真机联调时就是这么发现的）。
        """
        name = str(event.get("name") or "")
        path = str(event.get("path") or "")
        if not name or not path:
            return
        from_id = str(event.get("from_id") or "")
        from_name = str(event.get("from_name") or "") or "手机"
        self._add_history(name, path, "", peer_name=from_name, device_id=from_id)
        self._announce_local_upload(name, path, from_id, from_name)
        try:
            self.status_left.setText(f"收到「{from_name}」发来的文件：{name}")
        except Exception:
            pass

    def _announce_local_upload(self, name: str, path: str, from_id: str, from_name: str) -> None:
        """"手机发来的文件"也进会话 —— 传输表会被清空，会话记录才是能翻的地方。"""
        member = ""
        if from_id:
            # 用这台设备在会话里的规范写法（手机的 App 与浏览器归到同一个成员）。
            _key, canonical = self._member_device_key(from_id)
            member = canonical or from_id
        if not member:
            return
        conv_id = direct_conversation_id(self.engine.info.device_id, member)
        root = os.path.realpath(self.engine.config.receive_dir)
        target = os.path.realpath(path)
        rel = ""
        if target.startswith(root + os.sep):
            rel = os.path.relpath(target, root).replace(os.sep, "/")
        try:
            self.engine.chat.upsert_conversation(
                conv_id, kind="direct", members=[self.engine.info.device_id, member]
            )
        except Exception:
            pass
        peer = self._peer_by_device_id(from_id)
        self._announce_transfer_message(
            conv_id=conv_id,
            direction="in",
            path=path,
            name=name,
            peer=peer,
            state="done",
            rel=rel,
            sender_name=from_name,
            other_member=member,
        )

    @Slot(int)
    def _on_engine_started(self, port: int) -> None:
        self.port_label.setText(str(port))
        self._update_header()
        self._announce_ready()

    def _announce_ready(self) -> None:
        """把状态栏从「正在启动…」换成真实状态。

        引擎起得比窗口早时（绿色包启动、测试里先起 Engine 再建窗口），
        ``engine_started`` 那条事件在 bridge 接上之前就发完了，状态栏会一直挂着
        「正在启动…」——用户跑了 2 分钟看到的还是这句（宪章 E3：不许静默）。
        所以除了收事件，这里还兜一次底：真的起来了就自己说。
        """
        port = int(getattr(self.engine, "port", 0) or 0)
        if not port:
            return
        # 只在还挂着「正在启动…」时改写：用户这会儿可能已经点了什么，
        # 把人家刚看到的状态顶掉比不改更糟。
        if "正在启动" not in self.status_left.text():
            return
        self.port_label.setText(str(port))
        self.status_left.setText(self._ready_status_text())

    def _ready_status_text(self, devices: int = -1, phones: int = -1, channels: str = "") -> str:
        """「已就绪」那句话：设备数**必须跟着事实走**。

        它原来只在启动兜底时算一次，之后手机连上来了也不改 —— 真机实测里用户看到
        「发现 0 台设备」，而左边列表明明写着「设备列表 1 · 在线 1」（两个数字互相
        打脸）。现在设备刷新时会重算这一句。
        """
        port = int(getattr(self.engine, "port", 0) or 0)
        if devices < 0:
            try:
                devices = len(self._device_rows())
            except Exception:
                devices = 0
        if phones < 0:
            try:
                phones = len(self._phone_peers())
            except Exception:
                phones = 0
        if not channels:
            channels = self._discovery_channels_text()
        return (
            f"已就绪：正在监听 {port} 端口，发现 {devices} 台设备"
            + (f"、{phones} 台手机" if phones else "")
            + channels
            + " —— 在左边选一台开始。"
        )

    def _discovery_channels_text(self) -> str:
        """发现通道有几路在用（宪章 E2：出问题用户能自己看出来）。"""
        try:
            provider = getattr(self.engine, "diagnostics", None)
            if callable(provider):
                stats = (provider() or {}).get("channels") or {}
                healthy = [name for name, item in stats.items() if (item or {}).get("enabled")]
                if healthy:
                    return f"，发现通道 {len(healthy)}/{len(stats)} 在用"
        except Exception:
            pass
        return ""

    def _refresh_ready_status(self) -> None:
        """状态栏还挂着「已就绪…」时，把里面的设备数刷新到最新。

        只在它还挂着那一句时改写：用户这会儿可能已经点了什么（"正在发送…"），
        把人家刚看到的话顶掉比不改更糟。
        """
        try:
            if not self.status_left.text().startswith("已就绪"):
                return
            self.status_left.setText(self._ready_status_text())
        except Exception:
            pass

    @Slot(str)
    def _on_warning(self, message: str) -> None:
        self.status_left.setText(f"提示：{message}")

    # ------------------------------------------------------------------
    # 侧栏：本机信息 / 设备列表 / 会话列表
    # ------------------------------------------------------------------

    def _update_header(self) -> None:
        """Refresh the sidebar's self block and the settings page's fields."""
        info = self.engine.info
        port = self.engine.port
        addresses = []
        if port:
            for iface in list_interfaces(include_virtual=False):
                host = f"[{iface.address}]" if iface.is_ipv6 else iface.address
                addresses.append(f"{host}:{port}")
        self.sidebar.set_self_info(
            info.name, "  ·  ".join(addresses[:3]) if addresses else "正在检测网络…"
        )

        from ..core import crypto

        if crypto.CRYPTO_AVAILABLE and self.engine.config.encrypt:
            self.encryption_label.setText("🔒 已加密（Ed25519 + X25519 + ChaCha20-Poly1305）")
            self.encryption_label.setStyleSheet(f"color: {theme.SUCCESS};")
            self.sidebar.set_security("🔒 传输已加密", theme.SUCCESS)
        else:
            self.encryption_label.setText("⚠ 未加密（缺加密组件，内容以明文传输）")
            self.encryption_label.setStyleSheet(f"color: {theme.WARNING};")
            self.sidebar.set_security("⚠ 传输未加密", theme.WARNING)

        web_port = self.engine.config.web_port
        url = ""
        for iface in list_interfaces(include_virtual=False):
            if not iface.is_ipv6:
                url = f"http://{iface.address}:{web_port}/"
                break
        self.web_url_label.setText(url or "（未找到可用的局域网地址）")
        self.receive_dir_label.setText(self.engine.config.receive_dir)
        self.transfers_view.set_receive_dir(self.engine.config.receive_dir)

    def _on_chat_changed(self, event: dict) -> None:
        """A chat message arrived or was sent: refresh the session."""
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

    @Slot(list)
    def _on_conversations(self, rows: list) -> None:
        if rows and not getattr(self, "_folded_conversations", False):
            # 第一次拿到会话列表时折一次重名会话（手机换过写法的那种）。
            self._fold_conversations_soon()
        """ChatView 广播了会话列表：侧栏据此画未读/置顶，并补上离线会话。"""
        self._conversations = list(rows)
        groups = [row for row in rows if row.get("is_group")]
        self.sidebar.set_groups(groups, {row["id"]: row["title"] for row in groups})
        self._refresh_devices()

    def _on_conversation_changed(self, _conv_id: str) -> None:
        self._update_peer_header()

    # -- 设备行 -------------------------------------------------------------

    def _peer_aliases(self, peer: Peer) -> set[str]:
        """会话里可能用哪些 id 表示这台设备（手机有好几副面孔）。"""
        aliases = {peer.info.device_id}
        caps = peer.info.capabilities or {}
        # 同一台设备的**每一条**注册表记录键都是它自己的写法：影子记录
        # （页面首次加载那条）的 ``地址|UA摘要`` 也可能被写进会话成员里，
        # 认不出来就会多出一行"记录里的会话"（同一台手机两行）。
        for key in [str(caps.get("clientKey") or "")] + [
            str(item) for item in (caps.get("clientKeys") or [])
        ]:
            if key:
                aliases.update({key, "web:" + key, "android:" + key})
        ui = getattr(self, "_web_ui", None)
        if ui is not None:
            try:
                from ..core.chat import client_aliases

                for client in ui.known_clients():
                    if peer.info.device_id in client_aliases(client):
                        aliases |= client_aliases(client)
            except Exception:
                pass
        # 老记录里"地址 + UA 摘要"的两种写法（``地址|摘要`` 与 ``地址:摘要``）
        # 指的都是同一台设备：历史会话存的是哪一种都得认得出来，否则点设备行
        # 会又新建一段空会话（消息看着就"不见了"）。
        for alias in [item for item in aliases if item]:
            if "|" in alias:
                aliases.add(alias.replace("|", ":", 1))
            head, _, tail = alias.partition(":")
            if tail and ":" in tail:
                aliases.add(f"{head}:{tail.replace(':', '|', 1)}")
        return {alias for alias in aliases if alias}

    def _other_member(self, row: dict) -> str:
        mine = self.engine.info.device_id
        others = [member for member in row.get("members", []) if member and member != mine]
        return others[0] if others else ""

    def _fold_conversations_soon(self) -> None:
        """稍后再折：现在还在广播回调里，改数据库会让列表自己改自己。"""
        if getattr(self, "_fold_pending", False):
            return
        self._fold_pending = True

        def run() -> None:
            self._fold_pending = False
            if getattr(self, "_folded_conversations", False):
                return
            # 置位要在这里、而不是在计划阶段：这个开关的意义就是"自动整理只做一次"。
            # 以前它从来没被赋过 True（死代码），于是每来一次会话列表广播就会重新
            # 算一遍并查集、重新改一次库 —— 审计员在真实数据副本上看到的就是
            # "启动两秒后又并了一次"。
            self._folded_conversations = True
            try:
                self._fold_split_conversations()
            except Exception:
                pass

        QTimer.singleShot(800, run)

    def _conversation_rows(self) -> list[dict]:
        """会话列表：优先用 ChatView 广播过来的那份，没有就直接问一次。"""
        if self._conversations:
            return self._conversations
        try:
            return self.chat_view.conversation_rows()
        except Exception:
            return []

    def _conversation_for_peer(self, peer: Peer) -> str:
        """这台设备对应哪个会话（没有就返回空 —— 建不建由调用方决定）。"""
        mine = self.engine.info.device_id
        direct = direct_conversation_id(mine, peer.info.device_id)
        rows = self._conversation_rows()
        if any(row["id"] == direct for row in rows):
            return direct
        aliases = self._peer_aliases(peer)
        for row in rows:
            if row.get("is_group"):
                continue
            other = self._other_member(row)
            if not other:
                continue
            if other in aliases or other.endswith(peer.info.device_id) or peer.info.device_id.endswith(other):
                return str(row["id"])
        return ""

    def _peer_for_conversation(self, conv_id: str) -> Peer | None:
        """Conversation → the device it is with (``None`` for groups).

        Matching goes through the conversation's own member list rather than
        the cached row list: the header has to be right the moment a session is
        opened, which can be before the next list broadcast arrives.
        """
        if not conv_id:
            return None
        conversation = self.chat_view.conversation(conv_id) or {}
        if conversation.get("kind") == "group" or conv_id.startswith("g:"):
            return None
        mine = self.engine.info.device_id
        others = [
            str(member)
            for member in (conversation.get("members") or [])
            if member and str(member) != mine
        ]
        if len(others) != 1:
            return None
        member = others[0]
        for peer in self.device_table.peers:
            aliases = self._peer_aliases(peer)
            if member in aliases or member == peer.info.device_id:
                return peer
            if member.endswith(peer.info.device_id) or peer.info.device_id.endswith(member):
                return peer
        return None

    def _conversation_meta(self, peers: list[Peer]) -> dict[str, dict]:
        """peer.key → 未读 / 置顶 / 免打扰（设备行上要显示的东西）。"""
        meta: dict[str, dict] = {}
        for peer in peers:
            conv_id = self._conversation_for_peer(peer)
            row = next((r for r in self._conversations if r["id"] == conv_id), None)
            if row is None:
                continue
            meta[peer.key] = {
                "unread": int(row.get("unread") or 0),
                "pinned": bool(row.get("pinned")),
                "muted": bool(row.get("muted")),
            }
        return meta

    def _history_peer(self, member_id: str, row: dict) -> Peer:
        """A device we no longer see, but still have a conversation with.

        Without this the chat history became unreachable the moment the other
        machine was switched off -- the device list was the only way in.
        """
        ago = max(0.0, time.time() - float(row.get("updated") or 0))
        is_phone = member_id.startswith("web:") or member_id.startswith("android:")
        return Peer(
            info=DeviceInfo(
                device_id=member_id,
                name=str(row.get("title") or "未知设备"),
                kind="mobile" if is_phone else "desktop",
                platform="",
                capabilities={"online": False, "history": True, "secondsAgo": ago},
            ),
            address="",
            port=0,
            source="history",
            trusted=True,
        )

    # -- 以前见过的设备（离线那一栏的来源之一） --------------------------

    @property
    def _known_devices_path(self) -> str:
        return os.path.join(self.engine.config.data_dir, "known_devices.json")

    def _load_known_devices(self) -> None:
        try:
            with open(self._known_devices_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return
        if isinstance(data, dict):
            self._known_devices = {
                str(key): dict(value)
                for key, value in data.items()
                if isinstance(value, dict)
            }

    def _remember_device(self, peer: Peer) -> None:
        """记下这台设备的身份与**最后一次真的听到它**的时间。

        注意不是"这次刷新的时间"：引擎里的 ``last_seen`` 是单调时钟，换算成
        "多久以前"再落到墙上时间，离线那一行才会写「最后在线 1 小时前」，而不是
        "刚刚"（刷新一次就更新的写法会让每一台离线设备都显示成刚刚掉线）。
        """
        device_id = str(peer.info.device_id or "")
        if not device_id or device_id == self.engine.info.device_id:
            return
        now = time.time()
        age = max(0.0, time.monotonic() - float(peer.last_seen or 0))
        seen_at = now - age
        snapshot = {
            "name": peer.info.name,
            "kind": peer.info.kind,
            "platform": peer.info.platform,
            "version": peer.info.version,
            "device_id": device_id,
            "address": peer.address,
            "port": peer.port,
            "trusted": bool(peer.trusted),
            "web_port": int(peer.info.web_port or 0),
            "source": peer.source,
            "lastSeen": seen_at,
        }
        previous = self._known_devices.get(device_id)
        if previous == snapshot:
            return
        self._known_devices[device_id] = snapshot
        self._known_dirty = True

    def _prune_known_devices(self, now: float) -> None:
        for device_id, snapshot in list(self._known_devices.items()):
            if now - float(snapshot.get("lastSeen") or 0) > KNOWN_DEVICE_TTL:
                self._known_devices.pop(device_id, None)
                self._known_dirty = True
        if len(self._known_devices) > KNOWN_DEVICE_LIMIT:
            ordered = sorted(
                self._known_devices.items(), key=lambda item: float(item[1].get("lastSeen") or 0)
            )
            for device_id, _snapshot in ordered[: len(self._known_devices) - KNOWN_DEVICE_LIMIT]:
                self._known_devices.pop(device_id, None)
                self._known_dirty = True

    #: 落盘最短间隔。设备每 30 秒公告一次，每来一次就写盘没必要。
    KNOWN_SAVE_INTERVAL = 15.0

    def _save_known_devices(self, force: bool = False) -> None:
        if not self._known_dirty:
            return
        now = time.monotonic()
        if not force and now - getattr(self, "_known_saved_at", 0.0) < self.KNOWN_SAVE_INTERVAL:
            return
        self._known_saved_at = now
        self._known_dirty = False
        path = self._known_devices_path
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self._known_devices, fh, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
        except OSError:
            pass

    def _offline_peer(self, device_id: str, snapshot: dict, now: float) -> Peer:
        """一台「以前见过、现在不在线」的设备，长成普通设备行的样子。"""
        return Peer(
            info=DeviceInfo(
                device_id=device_id,
                name=str(snapshot.get("name") or "未知设备"),
                kind=str(snapshot.get("kind") or "desktop"),
                platform=str(snapshot.get("platform") or ""),
                version=str(snapshot.get("version") or ""),
                web_port=int(snapshot.get("web_port") or 0),
                capabilities={
                    "online": False,
                    "secondsAgo": max(0.0, now - float(snapshot.get("lastSeen") or 0)),
                    "seenBefore": True,
                },
            ),
            address=str(snapshot.get("address") or ""),
            port=int(snapshot.get("port") or 0),
            source="offline",
            trusted=bool(snapshot.get("trusted")),
        )

    def _device_rows(self) -> list[Peer]:
        peers = list(self.engine.devices()) + self._phone_peers()
        now = time.time()
        for peer in peers:
            if peer.source != "web":
                self._remember_device(peer)
        self._prune_known_devices(now)
        self._save_known_devices()
        known: set[str] = set()
        for peer in peers:
            known |= self._peer_aliases(peer)
        mine = self.engine.info.device_id
        # 走 _conversation_rows()（它会在缓存为空时直接问一次 ChatView）：
        # 设备列表不该取决于"聊天那次 1.5 秒的刷新有没有先跑过"。
        for row in self._conversation_rows():
            if row.get("is_group"):
                continue
            other = self._other_member(row)
            if not other or other == mine or other in known:
                continue
            peers.append(self._history_peer(other, row))
            known.add(other)
        # 以前见过、现在不在线的设备：身份来自落盘的快照（历史会话那条兜底
        # 只在"连记录都没有"时才用得上，两者不会给同一台设备生成两行）。
        for device_id, snapshot in self._known_devices.items():
            if device_id in known or device_id == mine:
                continue
            peers.append(self._offline_peer(device_id, snapshot, now))
            known.add(device_id)
        return self._with_display_names(peers)

    @staticmethod
    def _with_display_names(peers: list[Peer]) -> list[Peer]:
        """同名但身份不同的设备加一个序号后缀：``RealProbe`` / ``RealProbe #2``。

        三台真的不同的机器（deviceId 各不相同）都叫 ``RealProbe`` 时，按 deviceId
        去重是对的，但用户看到"同名三行"就会认为重复显示。消歧规则只有一份实现
        （``core.model.disambiguate_names``，三端共用），这里只负责把它套到显示名上；
        原始 Peer 不动 —— 会话标题、发送目标这些地方用的是同一份数据。
        """
        try:
            from ..core.model import disambiguate_names
        except Exception:  # pragma: no cover - core 还没导出时保持原样
            return peers
        try:
            names = disambiguate_names(peers)
        except Exception:  # pragma: no cover - 形状不认识时不猜
            return peers
        if not names:
            return peers
        out: list[Peer] = []
        for peer in peers:
            label = names.get(str(peer.info.device_id or ""))
            if not label or label == peer.info.name:
                out.append(peer)
                continue
            out.append(replace(peer, info=replace(peer.info, name=str(label))))
        return out

    def _remove_device(self, peer: Peer) -> None:
        """Forget a phone (it comes back the next time its page opens)."""
        caps = peer.info.capabilities or {}
        keys = [str(key) for key in (caps.get("clientKeys") or []) if str(key)]
        if not keys and caps.get("clientKey"):
            keys = [str(caps["clientKey"])]
        ui = getattr(self, "_web_ui", None)
        if ui is None or not keys:
            return
        # 删掉这台设备**所有**的注册表记录：只删一条的话，同一台手机的影子
        # 记录还会把它顶回设备列表里，用户看到的就是"删不掉"。
        removed = False
        for key in keys:
            try:
                removed = bool(ui.remove_client(key)) or removed
            except Exception:
                continue
        if removed:
            self._refresh_devices()
            self.status_left.setText(
                f"已移除「{peer.info.name}」——它下次打开页面会重新出现在这里。"
            )

    def _device_context_menu(self, peer: Peer, global_pos) -> None:
        """右键一台设备：会话偏好 + 忘记这台设备。"""
        menu = QMenu(self)
        conv_id = self._conversation_for_peer(peer)
        actions: dict = {}
        if conv_id:
            actions = self.chat_view.build_conversation_menu(conv_id, menu)
            menu.addSeparator()
        open_action = menu.addAction("打开会话")
        send_action = menu.addAction("发送文件给它…")
        menu.addSeparator()
        forget_action = None
        if peer.info.capabilities.get("web"):
            forget_action = menu.addAction(f"忘记「{peer.info.name}」")
        chosen = menu.exec(global_pos)
        if chosen is None:
            return
        if chosen is open_action:
            self._open_peer_session(peer)
        elif chosen is send_action:
            self._pick_files()
        elif chosen is forget_action:
            self._remove_device(peer)
        elif conv_id and actions:
            self.chat_view.apply_conversation_menu(chosen, actions, conv_id)

    # -- 打开会话 -----------------------------------------------------------

    def _open_peer_session(self, peer: Peer) -> None:
        """点侧栏的一台设备：主区切到会话，并把它作为发送目标。"""
        conv_id = self.chat_view.conversation_for_member(peer.info.device_id, f"和 {peer.info.name} 的对话")
        self.show_page("session", view="chat")
        self.session_view.header.set_peer(peer)
        self.chat_view.open_conversation(conv_id)

    def _open_group_session(self, conv_id: str) -> None:
        self.show_page("session", view="chat")
        self.chat_view.open_conversation(conv_id)

    def _reopen_live_conversation(self) -> bool:
        """换到一段"还在"的会话上（当前那段已经不存在时用）。

        整理重复会话时消息都搬进了保留的那段，被并掉的那段从库里消失；桌面端
        如果还"打开着"它，主区就会停在一个不存在的会话上 —— 顶上退回空状态、
        下面却还留着上一段会话的气泡。这里换到最近的一段上，让顶部和主区重新
        说同一件事。
        """
        if getattr(self, "_reopening", False):
            return False
        rows = self.chat_view.conversation_rows()
        if not rows:
            return False
        self._reopening = True
        try:
            # 程序替用户改开一段 ≠ 用户看过了：不标已读。
            self.chat_view.open_conversation(str(rows[0]["id"]), mark_read=False)
        finally:
            self._reopening = False
        return True

    def _conversation_header_text(self, conv_id: str, title: str = "") -> tuple[str, str]:
        """群聊顶部的两行字：名字 + 成员/时间，**同一个事实不写两遍**。

        以前标题写 `deadbeefca`、详情行又写一遍 `deadbeefca`（宪章 C9）。
        现在：有名字就用名字（``群聊（2 人）`` 或用户起的名），没名字（标题其实
        是个 id）才用成员名拼一个；详情行给成员名单或成员数 + 创建时间 + #短 id，
        标题里已经出现过的信息不再重复。
        """
        conversation = self.chat_view.conversation(conv_id) or {}
        members = [str(m) for m in (conversation.get("members") or []) if str(m)]
        names: list[str] = []
        for member in members:
            if member == self.engine.info.device_id:
                names.append("我")
                continue
            peer = self._peer_by_device_id(member)
            name = str(peer.info.name) if peer is not None and peer.info.name else ""
            if not name:
                name = "#" + member.split(":")[-1][:6]
            names.append(name)
        if not title or self._looks_like_an_id(title):
            title = "、".join(names[:3]) if names else "群聊"
            if len(names) > 3:
                title += f" 等 {len(names)} 人"
            subtitle_parts = [f"{len(members)} 个成员"] if members else ["群聊"]
        else:
            # 标题已经是「群聊（2 人）」这种人话：详情行就写成员名单，不重复人数。
            subtitle_parts = ["、".join(names[:4])] if names else []
        created = float(conversation.get("created") or conversation.get("updated") or 0)
        if created:
            subtitle_parts.append("创建于 " + time.strftime("%Y-%m-%d %H:%M", time.localtime(created)))
        short = conv_id.split(":")[-1][:8]
        if short and short not in title:
            subtitle_parts.append("#" + short)
        return title, " · ".join(part for part in subtitle_parts if part)

    def _known_peer_name(self, conv_id: str) -> str:
        """按会话成员设备号反查一个可读名字（页头别把内部 id 露给用户）。

        「会话标题其实是 device_id」时，页头以前直接写「未知设备 #hash」，
        而同一屏的消息卡片署名却是真名 —— 同一台设备两套名字。这里按可靠度
        依次查：设备表 → 传输事件缓存 → 手机（浏览器）客户端记录 → 会话里
        最后一条消息的署名。全都查不到才回退短号。
        """
        conversation = self.chat_view.conversation(conv_id) or {}
        self_id = str(self.engine.info.device_id)
        candidates: list[str] = []
        for member in [str(m) for m in (conversation.get("members") or []) if m]:
            raw = member[4:] if member.startswith("web:") else member
            if raw and raw != self_id and raw not in candidates:
                candidates.append(raw)
        if not candidates:
            tail = conv_id.split(":")[-1]
            raw = tail[4:] if tail.startswith("web:") else tail
            if raw and raw != self_id:
                candidates.append(raw)
        for device_id in candidates:
            peer = self._peer_by_device_id(device_id)
            if peer is not None and str(peer.info.name or "").strip():
                return str(peer.info.name).strip()
            for cached_id, cached_name in self._transfer_devices.values():
                if cached_id == device_id and cached_name:
                    return str(cached_name).strip()
        ui = getattr(self, "_web_ui", None)
        if ui is not None:
            try:
                for client in ui.known_clients():
                    if not any(device_id in str(client) for device_id in candidates):
                        continue
                    label = str(client.get("label") or client.get("name") or "").strip()
                    if label and not self._looks_like_an_id(label):
                        return label
            except Exception:
                pass
        try:
            for message in reversed(self.engine.chat.messages(conv_id, limit=40)):
                name = str(message.get("senderName") or "").strip()
                if name and not self._looks_like_an_id(name):
                    return name
        except Exception:
            pass
        return ""

    @staticmethod
    def _looks_like_an_id(text: str) -> bool:
        """``deadbeefca`` / ``g:1a2b3c`` 这种"其实是 id"的标题。"""
        stripped = str(text or "").split(":")[-1]
        if len(stripped) < 8:
            return False
        return all(char in "0123456789abcdef-" for char in stripped.lower())

    def _update_peer_header(self) -> None:
        """主区顶部永远显示"正在和谁说话"，同时让侧栏的选中行跟上。

        顶部和主区必须说同一件事：**只要有会话开着，就不显示「从左边选一台
        设备」那句空状态** —— 用户截图里的样子就是顶上让他去左边选设备、下面
        却已经摆着一条带图片的消息气泡。
        """
        conv_id = self.chat_view.current_conversation()
        peer = self._peer_for_conversation(conv_id)
        if peer is not None:
            self.session_view.header.set_peer(peer)
            self._sync_pin_requirement(peer)
            self._select_device_row(peer)
            return
        if not conv_id:
            self.session_view.header.set_peer(None)
            self._sync_pin_requirement(None)
            return
        if self.chat_view.conversation(conv_id) is None:
            # 这段会话已经不在了（被整理并掉、或记录被删）。
            if self._reopen_live_conversation():
                return
            # 没有别的会话可开：把"打开着"这件事一起收回来，让顶部和主区
            # 同时回到空状态（两处必须一致）。
            self.chat_view.open_conversation("", notify=False)
            self.session_view.header.set_peer(None)
            self._sync_pin_requirement(None)
            return
        title, people = self.chat_view.conversation_summary(conv_id)
        conversation = self.chat_view.conversation(conv_id) or {}
        is_group = str(conversation.get("kind") or "") == "group" or conv_id.startswith("g:")
        if is_group:
            # 群聊：标题给可读的名字，别把同一个 id 写两遍（宪章 C9）。
            title, people = self._conversation_header_text(conv_id, title)
            if title:
                self.session_view.header.set_conversation(title, people)
                self.sidebar.select_group(conv_id)
                self._sync_pin_requirement(None)
                return
        elif self._looks_like_an_id(title):
            # 一对一会话，但对端已经不在设备列表里了（只认记录）：
            # 这不是群聊，别给它挂「群聊」药丸。
            short = conv_id.split(":")[-1][:8]
            known = self._known_peer_name(conv_id)
            subtitle = "这台设备现在不在设备列表里；会话记录还在，它在的时候还会回到这一行。"
            self.session_view.header.set_conversation(
                known or f"未知设备 #{short}",
                subtitle,
                avatar="💬",
                pill="记录",
                pill_state="offline",
            )
            self._sync_pin_requirement(None)
            return
        if title:
            self.session_view.header.set_conversation(title, people)
            self.sidebar.select_group(conv_id)
        else:
            self.session_view.header.set_peer(None)
        self._sync_pin_requirement(None)

    def _sync_pin_requirement(self, peer: Peer | None) -> None:
        """对方要不要 PIN：只有在**需要**的时候才把输入框摆出来。

        ``ChatView`` 自己也会按注册表里的标记兜底判断；这里显式调一次是为了
        覆盖"刚收到一个需要 PIN 的报价"这种情况（宪章 §3.1：PIN 平时不占位）。
        """
        setter = getattr(self.chat_view, "set_pin_required", None)
        if not callable(setter):
            return
        caps = (peer.info.capabilities or {}) if peer is not None else {}
        needed = bool(str(caps.get("pin") or "").strip() or caps.get("needPin"))
        try:
            setter(needed)
        except Exception:
            pass

    def _select_device_row(self, peer: Peer) -> None:
        """侧栏高亮选中那台设备（会话是全局状态，侧栏和主区不能各说各话）。

        它可能在「离线」那一段里 —— 那就先把那一段展开，否则高亮行躲在折起来
        的地方，用户看不到自己选中了谁。
        """
        for table in (self.device_table, self.offline_table):
            for row, candidate in enumerate(table.peers):
                if candidate.key != peer.key:
                    continue
                if table is self.offline_table and not self.sidebar.offline_section.is_expanded():
                    self.sidebar.offline_section.set_expanded(True)
                if table.currentRow() != row:
                    table.setCurrentRow(row)
                return

    # ------------------------------------------------------------------
    # devices
    # ------------------------------------------------------------------

    #: 注册表记录里"这是哪台设备"的字段，按可信度从高到低。
    IDENTITY_FIELDS = ("identityKey", "deviceId")

    @classmethod
    def _client_identity(cls, client: dict, pool: list[dict] | None = None) -> str:
        """一条注册表记录的稳定身份（没有稳定身份就返回空串）。

        ``identityKey`` 是服务端给的"一台设备一条记录"的键；老记录只有页面
        自己生成的 ``deviceId``。

        给了 ``pool``（同一批记录）时还会做**影子记录回退**：这条记录没有设备号，
        但同一地址 + 同一 UA 上有一条带设备号的记录 —— 那就是它的原型（页面首次
        加载 `GET /` 不带自定义头，留下这么一条地址键记录）。两者必须算同一台
        设备，否则同一台手机的聊天记录会永远散在两段会话里。
        """
        for field in cls.IDENTITY_FIELDS:
            value = str(client.get(field) or "").strip()
            if value:
                return f"{field}:{value}"
        if not pool:
            return ""
        address = cls._client_address(client)
        agent = cls._client_agent(client)
        for other in pool:
            if other is client:
                continue
            if cls._client_address(other) != address:
                continue
            if agent and cls._client_agent(other) != agent:
                continue
            identity = cls._client_identity(other)
            if identity:
                return identity
        return ""

    @staticmethod
    def _client_address(client: dict) -> str:
        return str(client.get("address") or "")

    @staticmethod
    def _client_agent(client: dict) -> str:
        return str(client.get("agent") or "")

    @classmethod
    def _merge_client_group(cls, records: list[dict]) -> dict:
        """一组记录（同一台设备）→ 一条记录。字段优先级只写在这一处。

        规则：最新那条的地址/状态说了算；名字与版本取**带设备号那条**的非空值
        （影子记录的名字是 UA 猜的）；``online`` 取"任一条在线"；时间取最近
        的一条；两条记录的键都留着（忘记设备时一起删）。
        """
        newest = max(records, key=lambda client: float(client.get("lastSeen") or 0))
        # 带设备号/identityKey 的记录按新旧排：它是这台设备的"正式"身份。
        identified = sorted(
            (client for client in records if cls._client_identity(client)),
            key=lambda client: float(client.get("lastSeen") or 0),
            reverse=True,
        )

        def first_text(field: str) -> str:
            for pool in (identified, [newest], records):
                for client in pool:
                    if str(client.get(field) or "").strip():
                        return str(client.get(field))
            return ""

        merged = dict(newest)
        merged["label"] = first_text("label")
        for field in ("name", "version", "identityKey"):
            value = first_text(field)
            if value:
                merged[field] = value
        device_id = first_text("deviceId")
        if device_id:
            merged["deviceId"] = device_id
        else:
            merged.pop("deviceId", None)
        merged["kind"] = (
            "app"
            if any(str(client.get("kind") or "") == "app" for client in records)
            else str(newest.get("kind") or "")
        )
        merged["agent"] = next(
            (cls._client_agent(client) for client in records if cls._client_agent(client)), ""
        )
        merged["online"] = any(bool(client.get("online")) for client in records)
        merged["lastSeen"] = max(float(client.get("lastSeen") or 0) for client in records)
        ages = [
            float(client["secondsAgo"])
            for client in records
            if client.get("secondsAgo") is not None
        ]
        merged["secondsAgo"] = min(ages) if ages else 0.0
        keys = [str(client.get("key") or "") for client in records if str(client.get("key") or "")]
        merged["clientKeys"] = keys
        # 忘记设备时按"带设备号的那条"删：影子记录会跟着它一起消失。
        identified_keys = {str(client.get("key") or "") for client in identified}
        merged["clientKey"] = next(
            (key for key in keys if key in identified_keys), keys[0] if keys else ""
        )
        merged["address"] = cls._merged_address(records)
        return merged

    @staticmethod
    def _merged_address(records: list[dict]) -> str:
        """归并后的设备地址：最新那条的地址，回环地址不当作手机的地址。

        影子记录（页面首次加载那条）常常报 ``127.0.0.1``（走 adb 反代 / 端口
        转发进来的），把它当成手机的地址会让设备行写着一个不存在的"本机"。
        """
        newest = max(records, key=lambda client: float(client.get("lastSeen") or 0))
        if not bool(newest.get("isLocal")):
            return str(newest.get("address") or "")
        lan = [
            client
            for client in records
            if not bool(client.get("isLocal")) and str(client.get("address") or "")
        ]
        if lan:
            return str(max(lan, key=lambda c: float(c.get("lastSeen") or 0)).get("address") or "")
        return str(newest.get("address") or "")

    def _merge_client_records(self, clients: list[dict]) -> list[dict]:
        """把注册表记录按"这是哪台设备"并成一条 —— 一台设备只该占一行。

        同一台手机会同时留下两条记录：页面首次加载的 ``GET /`` 只带了
        「地址 + UA 摘要」（键形如 ``地址|UA摘要``），之后带设备号的请求又注册了
        一条（键 ``web:<设备号>``）。以前**每一条记录**都生成一台设备，于是侧栏里
        并排两台同名手机（"设备列表 2 / 在线 2"），点哪一台都进不去对方那段会话。

        身份优先级（前面能定就不看后面）：
          1. ``identityKey`` —— 服务端保证一台设备一条；
          2. ``deviceId``；
          3. 与某条带设备号的记录**地址 + UA** 都相同的那条影子记录；退一步，
             UA 相同且只有一条带设备号的记录时也算它（影子记录报的是回环地址，
             地址对不上但就是同一个页面）；
          4. 都没有才用记录键自成一台。

        返回的顺序与输入一致（第一条出现的位置），地址与名字的取舍见
        :meth:`_merge_client_group`。
        """
        by_address_agent: dict[tuple[str, str], str] = {}
        by_agent: dict[str, list[str]] = {}
        for client in clients:
            identity = self._client_identity(client)
            if not identity:
                continue
            by_address_agent.setdefault(
                (self._client_address(client), self._client_agent(client)), identity
            )
            agent = self._client_agent(client)
            if agent:
                by_agent.setdefault(agent, []).append(identity)

        groups: dict[str, list[dict]] = {}
        order: list[str] = []
        for client in clients:
            identity = self._client_identity(client)
            if not identity:
                agent = self._client_agent(client)
                identity = by_address_agent.get((self._client_address(client), agent), "")
                if not identity and agent and len(by_agent.get(agent, [])) == 1:
                    identity = by_agent[agent][0]
            if not identity:
                identity = "key:" + str(client.get("key") or "")
            if identity not in groups:
                groups[identity] = []
                order.append(identity)
            groups[identity].append(client)
        return [self._merge_client_group(groups[identity]) for identity in order]

    def _phone_peer(self, client: dict) -> Peer:
        """一条（已归并的）注册表记录 → 设备列表里的一行。"""
        address = self._client_address(client)
        agent = self._client_agent(client)
        # 安卓 App 和手机浏览器都会出现在这里，但它们是两回事：App 有设备号
        # 和版本，浏览器没有。以前统一写死成"（浏览器）/browser"，于是装了
        # App 的用户看到自己的手机被标成网页版。
        is_app = str(client.get("kind") or "") == "app"
        label = str(client.get("label") or ("安卓 App" if is_app else "手机"))
        device_id = str(client.get("deviceId") or "").strip()
        identity_key = str(client.get("identityKey") or "").strip()
        if device_id:
            # 手机在会话里的成员 id 就是 ``web:<设备号>``（见 core.chat.
            # client_aliases）：身份取设备号，点这一行打开的就是有消息的那段
            # 会话，而不是又新建一段空会话（那正是"会话散成几段"的来源）。
            device_id = f"web:{device_id}"
        elif identity_key:
            device_id = identity_key if ":" in identity_key else f"web:{identity_key}"
        else:
            digest = hashlib.sha1(agent.encode("utf-8", "replace")).hexdigest()[:8]
            device_id = f"web:{address}:{digest}"
        keys = [str(key) for key in (client.get("clientKeys") or []) if str(key)]
        if not keys and client.get("key"):
            keys = [str(client["key"])]
        return Peer(
            info=DeviceInfo(
                device_id=device_id,
                name=label if is_app else f"{label}（浏览器）",
                kind="mobile",
                platform="android" if is_app else "browser",
                version=str(client.get("version") or ("app" if is_app else "web")),
                web_port=self.engine.config.web_port,
                capabilities={
                    "web": True,
                    "handoff": True,
                    # The device row reads these to say 在线 / 未连接
                    # and "最后在线 x 分钟前".
                    "online": bool(client.get("online")),
                    "secondsAgo": float(client.get("secondsAgo") or 0),
                    "clientKey": str(client.get("clientKey") or client.get("key") or ""),
                    # 同一台设备的每一条注册表记录：忘记设备时全都要删，否则
                    # 影子记录还留着，那一行删不掉。
                    "clientKeys": keys,
                    "clientKind": "app" if is_app else "browser",
                },
            ),
            address=address,
            port=self.engine.config.web_port,
            source="web",
            trusted=True,  # it is the device the user just paired by QR
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
        # 一台设备一行：同一台手机可能同时留下"带设备号"和"只有地址+UA"两条记录。
        return [self._phone_peer(client) for client in self._merge_client_records(clients)]

    def _wire_open_failures(self) -> None:
        """「打开 / 打开所在文件夹」失败时要说出来。

        ``xdg-open`` 这类命令"进程起得来但退出码非 0"是常事（没有图形会话、没有
        关联程序），以前内核只看"起没起来"就返回成功，于是按钮点了没反应、也没有
        任何提示。内核现在会把失败原因交出来，这里把它接到状态栏上。
        """
        try:
            from ..core import platform_open
        except Exception:
            return

        def show(message: str) -> None:
            try:
                self.status_left.setText(message)
            except Exception:
                pass

        try:
            platform_open.set_failure_handler(show)
            # 已有失败记录（例如启动早期别处调用过一次）也要补上提示。
            pending = platform_open.last_open_error()
            if pending:
                show(pending)
        except Exception:
            pass

    def device_tables(self) -> list:
        """两段设备列表（在线 / 离线），需要遍历全部设备时用。"""
        return [self.device_table, self.offline_table]

    def selected_peers(self) -> list[Peer]:
        """两段（在线 / 离线）里选中的设备，按列表顺序合并。

        多选只能在**同一段**里点（Ctrl/Shift），跨段合并是为了"在离线段里点了一台
        之后按发送"这种情况：它不会被漏掉，也不会被算成两台。
        """
        found: list[Peer] = []
        for table in (self.device_table, self.offline_table):
            # 折叠着的那一段里可能还留着上一次的选中项（用户根本看不见它），
            # 那种"看不见的选中"不能算进发送目标 —— 用户看到「发送到 2 台」而
            # 屏幕上只选中了 1 台，就是这么来的。
            if not table.isVisible():
                continue
            for peer in table.selected_peers():
                if peer.key not in [p.key for p in found]:
                    found.append(peer)
        return found

    def _update_send_button(self) -> None:
        """Say how many devices the next 发送 will hit (multi-select)."""
        try:
            count = len([p for p in self.selected_peers() if peer_is_sendable(p)])
        except Exception:
            count = 0
        self.send_button.setText(f"发送到 {count} 台" if count > 1 else "发送")
        self.send_button.setEnabled(count > 0)

    def _refresh_devices(self) -> None:
        peers = self._device_rows()
        try:
            self.sidebar.set_devices(peers, self._conversation_meta(peers))
        except Exception:
            pass
        self._update_send_button()
        self._refresh_phone_shares()
        # 状态栏那句「发现 N 台设备」要跟着设备列表走（否则手机连上来了它还写着 0）。
        self._refresh_ready_status()
        self._update_peer_header()
        self._refresh_offline_count()
        if not self._restored_session:
            self._restored_session = True
            self._restore_session()

    def _restore_session(self) -> None:
        """打开上次那个会话（现在没有"上次"可存，就打开最近的一条）。

        恢复 ≠ 看过：这里**不标已读**（见 ``ChatView.open_conversation`` 的
        ``mark_read``）—— 一启动就把未读清掉，用户会以为自己漏看了消息。
        """
        if self.chat_view.current_conversation():
            return
        rows = self.chat_view.conversation_rows()
        if not rows:
            return
        self.chat_view.open_conversation(str(rows[0]["id"]), mark_read=False)

    def _announce(self) -> None:
        self.engine.announce()
        self.status_left.setText("正在广播自己的存在…")

    def _scan(self) -> None:
        self._scan_running = True
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
        self._scan_running = False
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

    # ------------------------------------------------------------------
    # staging files + sending
    # ------------------------------------------------------------------

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
            self.show_page("session", view="chat")
            total = ""
            try:
                total = human_bytes(self.file_list.total_size())
            except Exception:
                total = ""
            counted = f"{added} 项" + (f"（共 {total}）" if total else "")
            target = self._send_target_text()
            self.status_left.setText(
                f"已加入 {counted} · 回车{target}" if target
                else f"已加入 {counted} · 先从左边选一台设备"
            )
        self._update_total()

    def _send_target_text(self) -> str:
        """「发送」这一下会发给谁 —— 状态栏照着说，别对群聊说"选中的设备"。"""
        peers = [p for p in self.selected_peers() if peer_is_sendable(p)]
        if len(peers) == 1:
            return f"发给 {peers[0].info.name}"
        if len(peers) > 1:
            return f"发给选中的 {len(peers)} 台设备"
        conv_id = self.chat_view.current_conversation()
        conversation = self.chat_view.conversation(conv_id) or {}
        is_group = bool(conv_id) and (
            str(conversation.get("kind") or "") == "group" or conv_id.startswith("g:")
        )
        if is_group:
            members = len([m for m in (conversation.get("members") or []) if str(m)])
            return f"发到群聊（{members} 位成员）" if members else "发到群聊"
        return ""

    def _clear_files(self) -> None:
        self.file_list.clear_all()
        self._update_total()

    def _update_total(self) -> None:
        """统计（共 N 项 · X MB）由待发那一行自己算 —— 口径只有一份。"""
        self.file_list.refresh_stats()

    def _compose_send(self) -> None:
        """「发送」按钮：文字进会话，**文件走文件传输通道**。

        这里以前把选中的文件交给 ``chat_view.send_items()`` —— 那是聊天消息通道：
        文件被当成"带大附件的消息"投递（记一条 ``sent`` 就完事），手机永远收不到，
        电脑这边连 ``active_transfers()`` 都没有，于是没有进度、待发区不清空。
        真机结论见 ``docs/REAL_DEVICE_FINDINGS.md`` §2，别再改回去。
        """
        peers = [p for p in self.selected_peers() if peer_is_sendable(p)]
        paths = list(self.file_list.paths)
        text = self.chat_view.input.text().strip()
        if not peers and not text and not paths:
            QMessageBox.information(self, "还没有选择设备", "先在左边点一台设备，再发送。")
            return
        if peers:
            if len(peers) == 1:
                # 单台设备：打开和它的会话 —— 文字要进那段会话，文件的气泡和进度
                # 也长在那儿（"先把文件发到对话框，再显示进度"就是这个意思）。
                conv_peer = self._conversation_for_peer(peers[0])
                if not conv_peer or conv_peer != self.chat_view.current_conversation():
                    self._open_peer_session(peers[0])
                if text:
                    self.chat_view.input.clear()
                    self.chat_view.send_text(text)
            elif text:
                # 多选 = 群发：文字没法同时进多个一对一会话，只能进当前那段
                # （文件那部分照旧群发，每台一条传输记录）。
                if self.chat_view.current_conversation():
                    self.chat_view.input.clear()
                    self.chat_view.send_text(text)
                else:
                    self.status_left.setText(
                        "文字只能发到当前会话 —— 文件正在发给选中的设备。"
                    )
            if paths:
                # 留在会话页：文件气泡和它的进度就在用户刚说话的地方。
                self._send(stay_in_chat=True)
            elif not text:
                self.chat_view.input.setFocus()
            return
        # 没选中设备：文字还能发到当前打开的那段会话，文件不行 —— 文件传输
        # 一定要有一个对端，没对端就不是"发不出去"，而是"还没选人"。
        if text and self.chat_view.current_conversation():
            self.chat_view.input.clear()
            self.chat_view.send_text(text)
        if paths:
            self.status_left.setText(
                f"待发 {len(paths)} 个文件还没发：先在左边点一台设备，再按「发送」。"
            )
            self.chat_view.status_message("先在左边点一台设备，文件才有地方去。")
        if not self.chat_view.current_conversation():
            QMessageBox.information(
                self,
                "还没有选择设备",
                "先在左边点一台设备，再发送。",
            )
            return
        if text:
            self.show_page("session", view="chat")

    def _send(self, *, stay_in_chat: bool = False) -> None:
        """Send the chosen files to every selected device (multi-select).

        ``stay_in_chat=True`` 是输入区那个「发送」用的：文件要在**会话里**长出
        气泡和进度（用户是在会话里发的文件），所以不把页面翻到「传输」。
        """
        peers = [p for p in self.selected_peers() if peer_is_sendable(p)]
        if not peers:
            QMessageBox.information(self, "还没有选择设备", "请先在左边选择一台接收设备。")
            return
        if not self.file_list.paths:
            QMessageBox.information(self, "还没有选择文件", "请先添加要发送的文件或文件夹。")
            return
        if stay_in_chat:
            self.show_page("session", view="chat")
        else:
            self.show_page("session", view="transfers")
            self.transfers_view.show_direction("send")
        phone_peers = [p for p in peers if p.source == "web"]
        targets = [p for p in peers if p.source != "web"]
        if os.environ.get("EVERSEND_DEBUG_SEND"):
            for p in peers:
                print(
                    f"[send] peer={p.info.name!r} source={p.source!r} addr={p.address}:{p.info.tcp_port} "
                    f"kind={p.info.kind!r} caps={sorted((p.info.capabilities or {}).keys())}",
                    file=sys.stderr, flush=True,
                )
            print(f"[send] phone_peers={len(phone_peers)} targets={len(targets)}", file=sys.stderr, flush=True)
        for peer in phone_peers:
            self._hand_off_to_phone(peer)
        if not targets:
            self.file_list.clear_all()
            self._update_total()
            return
        for peer in targets:
            self._send_to_peer(peer)
        names = "、".join(p.info.name for p in targets)
        self.status_left.setText(
            f"正在发送到 {len(targets)} 台设备（{names}）…"
            + (f"，另有 {len(phone_peers)} 台手机在等它取文件" if phone_peers else "")
        )
        self.file_list.clear_all()
        self._update_total()

    def _send_to_peer(self, peer) -> None:
        """One transfer per target: each gets its own row and its own cancel."""
        pending_key = f"__pending__{time.time()}-{peer.info.device_id[:6]}"
        paths = list(self.file_list.paths)
        # 同一个文件再发给同一台设备 = 同一行（宪章 C8）。键要在建行之前算好，
        # 不然第二次投递会先长出第二行、再去合并它。
        record_key = self._paths_record_key("out", peer.info.device_id, paths)
        row = self._add_transfer_row(pending_key, f"发送到 {peer.info.name}", "send", key=record_key)
        row.set_status("正在发送请求…")
        # 记住这次发的是哪几个文件：失败之后行里的「重试」要按原样再发一次。
        row.set_paths(paths)
        worker = SendWorker(self.engine, peer, paths, self.pin_edit.text())
        # 会话里也留一条记录（「传输」页和会话必须是同一笔账）。
        self._announce_sent_files(peer, paths, pending_key)

        # SendWorker runs on a plain thread, so it only emits; the queued
        # connection delivers the call on the GUI thread.
        worker.on_done = lambda ok, error, key=pending_key: self.send_finished.emit(ok, error, key)
        self._send_workers.append(worker)
        self._send_workers = [w for w in self._send_workers if w.is_alive() or w is worker]
        worker.start()
        self._transfers[pending_key] = row
        # 「重试」要知道这一行原本发给谁 —— 名字是显示用的，重发得用 Peer。
        row.retry_peer = peer
        # Only one row can be "the pending row" for adoption; the rest are
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
            elif self._was_cancelled(key):
                # 发送线程取消回来的是 (False, "")：这是用户自己点的，不是故障。
                self._finish_row(key, False, "已取消")
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
        paths = [str(p) for p in self.file_list.paths]
        ui = getattr(self, "_web_ui", None)
        if ui is None:
            reason = "浏览器界面没有启动（手机就是从它连上来的），文件没法交给手机"
            self._fail_paths_in_chat(peer, paths, reason)
            self.status_left.setText(f"没有发给手机：{reason}")
            QMessageBox.warning(self, "交给手机失败", reason)
            return
        # 读不到的文件绝不能静默消失：``share_files`` 会跳过它们，不先记下名字，
        # 用户看到的就是"发了、什么都没发生"。
        unreadable = [p for p in paths if not os.path.isfile(p)]
        readable = [p for p in paths if os.path.isfile(p)]
        entries = ui.share_files(readable)
        shared = {os.path.abspath(str(entry.get("path") or "")) for entry in entries}
        unreadable += [p for p in readable if os.path.abspath(p) not in shared]
        if unreadable:
            names = "、".join(os.path.basename(p) or p for p in unreadable[:3])
            more = f" 等 {len(unreadable)} 个" if len(unreadable) > 3 else ""
            self._fail_paths_in_chat(peer, unreadable, f"这些文件读不到，没法发给手机：{names}{more}")
        if not entries:
            reason = "选中的文件都读不到了，没有东西可以交给手机"
            self.status_left.setText(f"没有发给手机：{reason}")
            QMessageBox.warning(self, "没有可发送的文件", reason)
            return
        online = bool(peer.info.capabilities.get("online"))
        if not self.chat_view.current_conversation():
            # 会话没打开的话，用户看不到刚发出去的文件气泡与进度 —— 打开它。
            self._open_peer_session(peer)
        for entry in entries:
            share_id = str(entry.get("id") or "")
            path = str(entry.get("path") or "")
            size = int(entry.get("size") or 0)
            name = str(entry.get("name") or os.path.basename(path))
            row = self._add_transfer_row(share_id, f"交给 {peer.info.name}：{name}", "send")
            row.set_paths([path])
            handoff = getattr(row, "set_phone_handoff", None)
            waiting_reason = "" if online else "手机现在不在线"
            if callable(handoff):
                # 手机通道：进度条是**待定态**（"等它下载"），不是 0% 卡住。
                handoff(total_bytes=size, waiting_reason=waiting_reason)
            else:
                row.set_total(size)
                if online:
                    row.bar.setRange(0, 0)
                    row.set_status("已交给手机，等它下载")
                else:
                    offline = getattr(row, "set_phone_offline", None)
                    if callable(offline):
                        offline(waiting_reason)
                    else:
                        row.bar.setRange(0, 0)
                        row.set_status("手机现在不在线（熄屏？）——文件已留着，等它回来下载")
            self._transfers[share_id] = row
            self._phone_shares[share_id] = row
            self._phone_share_paths[share_id] = (path, peer)
            self._phone_share_watch.pop(share_id, None)
            # 会话里那一笔：**先把文件放进对话框**，状态是 sending ——
            # 手机真正取走之前绝不写 sent（见 _refresh_phone_shares）。
            # 记录不带 mediaRel（那是"接收目录里的相对路径"，发给手机的文件不在
            # 那儿）；手机点气泡上的「保存」时，服务端按 mediaSource 在分享表里
            # 找到同一份文件、走分享通道发出去（web/server.py 的
            # ``find_share_for_message``，2026-09 与 web-ui 定死的口径）。
            self._announce_sent_files(peer, [path], share_id)
            self.chat_view.set_transfer_progress(
                name,
                None,
                f"已交给手机，等它下载（{human_bytes(size)}）" if size else "已交给手机，等它下载",
                state_text=waiting_reason or "已交给手机，等它下载",
            )
        if online:
            self.status_left.setText(
                f"已把 {len(entries)} 个文件交给手机页面——请在手机上点「下载」。"
            )
        else:
            self.status_left.setText(
                f"已把 {len(entries)} 个文件留给手机（它现在不在线）——手机打开页面就能下载。"
            )

    def _refresh_phone_shares(self) -> None:
        """手机正在取文件时，把**真实进度**同时写进传输表和会话里那一条。

        以前这里只在"取完了"的时候改一次状态，中间什么都不显示 —— 一个 800 MB 的
        文件在手机上下载着，电脑这边看起来像什么都没发生。现在服务端一边发字节
        一边报进度（``note_share_progress``），这里把它换算成百分比：
          · 传输表的行：进度条 + 「手机正在取 42% · 120 MB / 572 MB」；
          · 会话里那条记录：同样的百分比（用户是在会话里发的文件，就该在那儿看到）。

        **状态必须诚实**：``downloaded`` 为真之前，会话里那条记录一直是
        ``sending``；手机真的取走了才落 ``sent``（§2.4.2）。分享条目消失、文件
        读不到这类真失败，给出中文原因，绝不静默成功。
        """
        if not self._phone_shares:
            return
        ui = getattr(self, "_web_ui", None)
        if ui is None:
            return
        try:
            shares = {s["id"]: s for s in ui.shares()}
        except Exception:
            return
        now = time.monotonic()
        for share_id, row in list(self._phone_shares.items()):
            share = shares.get(share_id)
            done = bool(getattr(row, "_phone_done", False))
            if share is None:
                # 分享表里没了（电脑重启过 / 被裁剪掉 / 用户清空了分享）。
                if not done:
                    self._phone_share_failed(
                        share_id, row, "这个文件已经从电脑的分享列表里消失了（电脑重启过？）——请重新发一次"
                    )
                continue
            total = int(share.get("size") or 0)
            sent = int(share.get("sentBytes") or 0)
            name = str(share.get("name") or "")
            percent = max(0.0, min(100.0, 100.0 * sent / total)) if total > 0 else 0.0
            if bool(share.get("downloaded")):
                self.chat_view.set_transfer_progress(
                    name, 100.0, "✅ 手机已取走", state_text="已发送"
                )
                if not done:
                    taken = getattr(row, "set_phone_taken", None)
                    if callable(taken):
                        taken()
                    else:
                        row.bar.setRange(0, 1000)
                        row.bar.setValue(1000)
                        row.finish(True, "手机已取走")
                    row._phone_done = True  # noqa: SLF001 - our own marker
                    # 手机真的取走了 —— 只有到这一步才写 sent。
                    self._finish_transfer_messages(share_id, True)
                    self.status_left.setText(f"手机已经取走「{name}」。")
                    self._phone_share_watch.pop(share_id, None)
                    if os.environ.get("EVERSEND_DEBUG_PROGRESS"):
                        print(
                            f"[phone] share={share_id} name={name!r} taken sent={sent}/{total}",
                            file=sys.stderr,
                            flush=True,
                        )
                continue
            if done or total <= 0:
                continue
            if sent <= 0:
                # 还没开始取：说清楚"在等它"，不要显示一个空进度条让人以为卡住了。
                self.chat_view.set_transfer_progress(
                    name,
                    None,
                    f"已交给手机，等它下载（{human_bytes(total)}）",
                    state_text="已交给手机，等它下载",
                )
                continue
            progress = getattr(row, "set_phone_progress", None)
            if callable(progress):
                progress(sent, total)
            else:
                row.bar.setRange(0, 1000)
                row.bar.setValue(int(percent * 10))
                row.set_status(f"手机正在取 {percent:.0f}%")
            text = f"⬇ 手机正在取 {percent:.0f}% · {human_bytes(sent)} / {human_bytes(total)}"
            # 手机取到一半不动了：说清"停住了"，别让电脑这边以为还在慢慢传。
            last_sent, last_when = self._phone_share_watch.get(share_id, (0, now))
            if sent != last_sent:
                self._phone_share_watch[share_id] = (sent, now)
            elif now - last_when >= 30.0:
                text += "（已停住 30 秒以上：手机可能切到后台或掉线了，文件还在，可以再点一次下载继续）"
            self.chat_view.set_transfer_progress(
                name, percent, text, state_text=f"手机正在取 {percent:.0f}%"
            )
            if os.environ.get("EVERSEND_DEBUG_PROGRESS"):
                print(
                    f"[phone] share={share_id} name={name!r} sent={sent}/{total} "
                    f"pct={percent:.1f}",
                    file=sys.stderr,
                    flush=True,
                )

    def _phone_share_failed(self, share_id: str, row: TransferRow, reason: str) -> None:
        """手机这条路上的**真失败**：行变红药丸 + 中文原因，会话里那条也改成失败。"""
        failed = getattr(row, "set_phone_failed", None)
        if callable(failed):
            failed(reason)
        else:
            row.finish(False, reason)
        row._phone_done = True  # noqa: SLF001 - our own marker（终态，别再刷回去）
        self._fail_transfer_messages(share_id, reason)
        self._phone_share_watch.pop(share_id, None)
        self.status_left.setText(f"发给手机失败：{reason}")

    # ------------------------------------------------------------------
    # transfers
    # ------------------------------------------------------------------

    def _adopt_pending_row(self, transfer_id: str, title: str, direction: str) -> TransferRow | None:
        """Reuse the placeholder row for the transfer that just started.

        The row starts under a placeholder key (``__pending__…``) because the
        real transfer id only exists once the session is up.  Renaming it is
        what let 「取消」 point at the right transfer -- but it also meant the
        send worker's ``on_done`` could no longer find the row by its old key,
        so a cancelled send sat at 「正在取消…」 forever while the engine had
        already let it go.  Both keys therefore keep pointing at the same row
        until it finishes.
        """
        key = getattr(self, "_pending_row_key", None)
        row = self._transfers.get(key) if key else None
        if row is None:
            return None
        self._pending_row_key = None
        if key:
            self._row_aliases[transfer_id] = key
        # 行句柄与它模型里那一格都要换成真正的传输号：自绘的操作格是按
        # ``item.transfer_id`` 去引擎里找这次传输的，只改行句柄会让「取消」
        # 打在旧号上（引擎说"没有这次传输"，卡片就永远停在「正在取消…」）。
        row.set_transfer_id(transfer_id)
        row.title.setText(title)
        try:
            row.cancel_button.clicked.disconnect()
        except (RuntimeError, TypeError):
            pass
        row.cancel_button.clicked.connect(lambda _c=False, t=transfer_id: self._cancel_transfer(t))
        self._transfers[transfer_id] = row
        return row

    def _finish_row(self, key: str, ok: bool, message: str) -> bool:
        """Finish the row for ``key`` (a transfer id *or* a placeholder key).

        Finishing has to be idempotent and has to work from either name,
        because the two things that end a send -- the ``send_finished`` event
        and the worker thread's callback -- know it by different ones.
        """
        # 记下"这个 id 已经结束"。结束事件比"从 active_transfers() 里消失"早到，
        # 中间那几百毫秒里进度轮询会以为它还在传、又建一行 —— 那一行再也没人
        # 收尾，就永远写着「传输中」。以前它表现为两张重复卡片，换成一表一行
        # 之后就变成一行赖着不走（清空记录也收不走它）。
        now = time.monotonic()
        self._finished_ids[key] = now
        if not ok and self._was_cancelled(key):
            # 用户自己点的取消：**不是故障**（宪章：红色会让人以为出错了）。
            # 这里不看 message 里有没有「取消」两个字 —— 发送线程那条路传的是空串。
            message = "已取消"
        row = self._transfers.get(key)
        if row is None:
            for transfer_id, pending in self._row_aliases.items():
                if transfer_id == key or pending == key:
                    row = self._transfers.get(transfer_id) or self._transfers.get(pending)
                    break
        if row is None:
            return False
        self._finished_ids[str(getattr(row, "transfer_id", "") or key)] = now
        if getattr(row, "_finished", False):
            return True
        # ``TransferRow.finish`` owns the "already finished" flag: marking it
        # here first made the widget skip the very update we came for, so the
        # row kept saying 「正在取消…」 while everything else had moved on.
        row.finish(ok, message)
        # 收完的文件在这一行和历史行里各显示一次（用户看到的"同一个文件占两行"）。
        # 文件已经落盘、历史行带着完整动作，这一行就该收起来。
        if str(getattr(row, "direction", "")) == "recv" and key in self._history_transfers:
            row.hide()
        # The row can be known by two names; forget both so no stale key can be
        # finished twice.
        for name, mapped in list(self._transfers.items()):
            if mapped is row:
                self._finished_ids[name] = now
                self._transfers.pop(name, None)
        for transfer_id, pending in list(self._row_aliases.items()):
            if self._transfers.get(transfer_id) is None and self._transfers.get(pending) is None:
                self._row_aliases.pop(transfer_id, None)
        self._forget_cancelled(key)
        self.transfers_view.refresh_empty()
        return True

    def _was_cancelled(self, key: str) -> bool:
        """这个传输号（或它的占位别名）是不是用户取消的。"""
        if key in self._cancelled_ids:
            return True
        alias = self._row_aliases.get(key)
        if alias and alias in self._cancelled_ids:
            return True
        for real, pending in self._row_aliases.items():
            if pending == key and real in self._cancelled_ids:
                return True
        return False

    def _forget_cancelled(self, key: str) -> None:
        self._cancelled_ids.discard(key)
        alias = self._row_aliases.get(key)
        if alias:
            self._cancelled_ids.discard(alias)

    def _add_transfer_row(
        self, transfer_id: str, title: str, direction: str, key: str = ""
    ) -> TransferRow:
        """给一次传输开一行（同一个传输号 / 同一个键**复用**已有那一行）。

        宪章 C8：一个文件在一张表里只占一行。判据是 ``key`` —— 由 core 给的
        ``transfer_record_key(方向, 对端, 内容)`` 算出来的稳定摘要；没给 key 时
        退回传输号本身（同一次传输被报两次也是同一行）。
        """
        existing = self._transfers.get(transfer_id)
        if (
            existing is not None
            and not getattr(existing, "_finished", False)
            and str(getattr(existing, "direction", "")) == direction
        ):
            return existing
        table = self.transfers_view.table(direction)
        row = TransferRow(transfer_id, title, direction, table, key=key)
        row.cancel_requested.connect(self._cancel_transfer)
        self._transfers[transfer_id] = row
        if direction == "recv":
            self._recv_from[transfer_id] = row.peer_name
        self.sidebar.set_transfer_count(len(self._transfers))
        self.transfers_view.refresh_empty()
        return row

    # -- C8：一次投递的稳定身份 ---------------------------------------------

    def _content_identity(self, *, path: str = "", name: str = "", size: int = 0) -> str:
        """"传的是什么"：优先内容摘要，拿不到就用 (大小, mtime, 名字)。

        摘要走引擎的 ``digest_cache``（引擎每传一个文件都会算一次，重复查询是
        查表），并且限制在 8 MiB 以内 —— 界面线程不能为了给一行建索引去读几个 G。
        """
        try:
            from ..core.chat import content_identity
        except Exception:
            return ""
        try:
            return content_identity(
                path=path,
                name=name,
                size=int(size or 0),
                cache=getattr(self.engine, "digest_cache", None),
                max_bytes=8 * 1024 * 1024,
            )
        except Exception:
            return ""

    def _record_key(self, direction: str, peer_identity: str, identity: str) -> str:
        """``(方向, 对端, 内容)`` → 稳定键（core 里那一个实现，三端同一套）。"""
        if not identity:
            return ""
        try:
            from ..core.chat import transfer_record_key
        except Exception:
            return ""
        try:
            return transfer_record_key(direction, str(peer_identity or ""), identity)
        except Exception:
            return ""

    def _paths_record_key(self, direction: str, peer_identity: str, paths: list) -> str:
        """一次群发（可能多个文件）的内容身份：各文件身份拼起来再摘要。"""
        parts: list[str] = []
        for path in paths:
            try:
                size = os.path.getsize(path)
            except OSError:
                size = 0
            identity = self._content_identity(
                path=path, name=os.path.basename(path), size=size
            )
            if identity:
                parts.append(identity)
        if not parts:
            return ""
        return self._record_key(direction, peer_identity, "|".join(sorted(parts)))

    def _refresh_devices_now(self) -> None:
        """按下侧栏那个「扫描」：先催一次公告，没回应时自己升级成主动扫描。

        以前这是两个按钮（⟳ 刷新 / ◎ 扫描）：用户得先点一个、等几秒、没反应
        再点另一个 —— 那正是"操作逻辑繁复无意义"。现在一个按钮干两件事，并且
        **自己判断**要不要升级：催完公告 1.5 秒，如果一台新设备都没多出来，
        就说明对方听不见广播（防火墙 / 跨网段 / 客户端隔离），这时逐个探测
        整个网段才有意义（宪章 §5：扫描是加速手段，不是必需步骤）。
        """
        before = len(self._device_rows())
        try:
            self.engine.announce()
        except Exception:
            pass
        self._refresh_devices()
        phones = self._phone_peers()
        self.status_left.setText(
            f"正在扫描局域网：已催一次公告，{len(self.engine.devices())} 台协议设备、"
            f"{len(phones)} 台手机…"
        )

        def escalate() -> None:
            try:
                after = len(self._device_rows())
            except Exception:
                after = before
            if after > before or getattr(self, "_scan_running", False):
                self.status_left.setText(f"扫描完成：共 {after} 台设备（已列在左边）")
                return
            # 公告没人应答：这才是主动扫描的用武之地。
            self._scan()

        self._scan_running = False
        QTimer.singleShot(1500, escalate)

    def _cancel_transfer(self, transfer_id: str) -> None:
        accepted = False
        try:
            accepted = bool(self.engine.cancel(transfer_id, "用户取消"))
        except Exception as exc:
            self.status_left.setText(f"取消失败：{exc}")
        if accepted:
            # 记下"这一次是用户取消的"：真正收尾的回调可能来自发送线程
            # （``on_done(False, "")``，没有原因字符串），也可能来自引擎事件。
            # 两个名字都记：「真传输号」和它接手前的「占位名」。
            self._cancelled_ids.add(transfer_id)
            alias = self._row_aliases.get(transfer_id)
            if alias:
                self._cancelled_ids.add(alias)
            for real, pending in self._row_aliases.items():
                if pending == transfer_id:
                    self._cancelled_ids.add(real)
            row = self._transfers.get(transfer_id)
            if row is not None:
                row.set_status("正在取消…")
                # A second click would only ask again for something already on
                # its way out; the button comes back with the next transfer.
                row.cancel_button.setEnabled(False)
            return
        # Nothing to cancel: the transfer already ended (or was never active).
        # Saying 「正在取消…」 here is what left the row spinning forever.
        self._finish_row(transfer_id, False, "已取消")

    def _refresh_progress(self) -> None:
        """Poll live transfers and update their rows."""
        try:
            active = self.engine.active_transfers()
        except Exception:
            return
        now = time.monotonic()
        # 已经结束的 id 短期记住：引擎还会把它们列在 active 里几百毫秒，
        # 那期间不能给它们再建一行（建了就没人收尾）。
        for stale in [key for key, when in self._finished_ids.items() if now - when > 30.0]:
            self._finished_ids.pop(stale, None)
        started: str = ""
        for transfer in active:
            if transfer.transfer_id in self._finished_ids:
                continue
            peer_info = getattr(transfer, "peer", None)
            if peer_info is not None and getattr(peer_info, "device_id", ""):
                # 接收事件里只有传输号；把对端设备号记在传输号下面，
                # 收完的"给会话留一条记录"才找得到是哪台设备发来的。
                self._transfer_devices[transfer.transfer_id] = (
                    str(peer_info.device_id),
                    str(getattr(peer_info, "name", "") or ""),
                )
            row = self._transfers.get(transfer.transfer_id)
            if row is None:
                # 引擎方向词汇是 ``receive``/``send``，桌面表用 ``recv``/``send``
                # （见 transfers_view 的 RECV_COLUMNS、_recv_from、_finish_row
                # 的方向判断）。直接透传引擎词汇会让「收尾行隐藏」「_recv_from
                # 记录来源」等一整套逻辑静默失效——每次接收完都会多留一行
                # 「完成」，而且断线/失败时找不到来源设备。
                direction = "send" if transfer.direction == "send" else "recv"
                label = ("发送到 " if direction == "send" else "接收自 ") + transfer.peer.name
                row = self._adopt_pending_row(
                    transfer.transfer_id, label, direction
                ) or self._add_transfer_row(transfer.transfer_id, label, direction)
                row.set_status("传输中")
                started = direction
            names = [
                os.path.basename(str(getattr(item.entry, "name", "") or item.path or ""))
                for item in transfer.items.values()
            ]
            names = [name for name in names if name]
            if names:
                row.set_files(_files_summary(names))
            stats = transfer.stats
            done = stats.done_bytes
            total = max(1, stats.total_bytes)
            row.set_total(total)
            row.update_progress(done, total, stats.instant_speed_bps, stats.eta_seconds)
            # 字节已满但传输还没结束 = 落盘/摘要校验的收尾阶段（实测 512MiB 在
            # 100% 之后还停留 1.5~7.6s，越闲越久）。这段时间继续显示「传输中」
            # 并把窗口内均值速度（从 320MB/s 一路衰减到 55MB/s）摆出来是在说
            # 假话：明确标「校验中…」，速度/剩余置空，结束事件会再覆盖成终态。
            if (
                stats.total_bytes > 0
                and done >= stats.total_bytes
                and not getattr(row, "_finished", False)
            ):
                row.set_status("校验中…")
                row.item.speed_bps = 0.0
                row.item.eta = None
                row.touch()
        self._last_tick = now
        self.sidebar.set_transfer_count(
            len([t for t in active if t.transfer_id not in self._finished_ids])
        )
        if started and self.main_stack.currentWidget() is self.session_view:
            # 传输刚开始时把对应的那张表翻到前面（用户已经在看传输页的话）。
            if self.session_view.current_view() == "transfers":
                self.transfers_view.show_direction(started)
        if not active:
            self.transfers_view.refresh_empty()
        # 手机那条路没有 active_transfers（手机是 HTTP 客户端，电脑推不过去），
        # 所以进度要在同一个 400ms 的 tick 里一起刷 —— 只挂在 2 秒的设备刷新上，
        # 用户看到的就是"进度一格一格跳"。
        self._refresh_phone_shares()

    def _clear_transfers(self) -> None:
        """清空记录：把已经结束的传输行收起来，正在传的不动。"""
        for name, row in list(self._transfers.items()):
            if getattr(row, "_finished", False):
                row.deleteLater()
                self._transfers.pop(name, None)
        hidden = 0
        for table in (self.transfers_view.recv_table, self.transfers_view.send_table):
            hidden += table.hide_all()
        self._recv_from.clear()
        self.transfers_view.refresh_empty()
        self.status_left.setText(
            f"已清空 {hidden} 条传输记录（正在传的不受影响）" if hidden else "没有可以清空的传输记录"
        )

    def _clear_current(self) -> None:
        """顶部按钮：会话页删掉这个会话，传输页清空记录。"""
        if self.session_view.current_view() == "transfers":
            self._clear_transfers()
            return
        conv_id = self.chat_view.current_conversation()
        if not conv_id:
            self.status_left.setText("没有打开的会话。")
            return
        title, _people = self.chat_view.conversation_summary(conv_id)
        answer = QMessageBox.question(
            self,
            "删除这个会话",
            f"把本机保存的「{title or conv_id}」聊天记录删掉？\n"
            "对方那边的记录不受影响，设备也还在列表里。",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return
        try:
            self.engine.chat.remove_conversation(conv_id)
        except Exception as exc:
            QMessageBox.warning(self, "删除失败", str(exc))
            return
        self.chat_view.open_conversation("")
        self.chat_view.reload()
        self.status_left.setText("会话已删除。")

    # ------------------------------------------------------------------
    # 行内动作：重试 / 重新发送 / 去发送
    # ------------------------------------------------------------------

    def _go_compose(self) -> None:
        """空状态里的「去发送一个文件」：回到会话，把光标放进输入框。"""
        self.show_page("session", view="chat")
        try:
            self.chat_view.input.setFocus()
        except Exception:
            pass
        self.status_left.setText("在左边选一台设备，再点「传输文件」选要发的东西。")

    def _retry_transfer(self, transfer_id: str) -> None:
        """失败行里的「重试」（宪章 C5/C14：失败要给中文原因 + 重试）。

        · 发送方向：把当时那几个文件重新排进待发，**真的再传一次**给同一台设备；
        · 接收方向：文件是对方推过来的，本机没法主动去拉，所以如实说清下一步
          该做什么（而不是让按钮点了没反应）。
        """
        row = self._transfers.get(transfer_id)
        if row is None:
            row = next(
                (item for item in self._transfers.values() if getattr(item, "transfer_id", "") == transfer_id),
                None,
            )
        if transfer_id in self._phone_shares:
            # 手机那条路只能"重新发布一次、让它再取" —— 往手机上推是推不过去的。
            self._retry_phone_share(transfer_id)
            return
        item = getattr(row, "item", None)
        reason = str(getattr(item, "reason", "") or "")
        if row is not None and str(getattr(row, "direction", "")) == "recv":
            self.status_left.setText(
                f"接收这次没成功（{reason or '原因不明'}）——请让对方重新发一次，"
                "已经传过来的部分还在，重发会接着传。"
            )
            return
        paths = [
            path
            for path in list(getattr(item, "paths", []) or [])
            if path and os.path.exists(path)
        ]
        peer = getattr(row, "retry_peer", None) if row is not None else None
        if peer is None or not paths:
            self.status_left.setText("这一行没有可以重发的源文件（文件可能已经被移走或删掉了）。")
            return
        self.file_list.clear_all()
        self.file_list.add_paths(paths)
        self._update_total()
        self.show_page("session", view="transfers")
        self._send_to_peer(peer)
        self.status_left.setText(f"正在重发 {len(paths)} 个文件给 {peer.info.name}…")

    def _retry_phone_share(self, share_id: str) -> None:
        """手机那条路的重试：**重新发布一次**，等它再取（推不过去，别假装能推）。

        旧那一条分享留着也没用（手机那边已经有它的记录了），所以把旧行收起来、
        用新的分享号建一行新的 —— 进度与状态从"等它下载"重新开始。
        """
        path, peer = self._phone_share_paths.get(share_id, ("", None))
        row = self._phone_shares.get(share_id)
        if row is None or peer is None or not os.path.isfile(path):
            self.status_left.setText("这个文件已经不在原处了，没法重新发给手机。")
            return
        self._phone_shares.pop(share_id, None)
        self._phone_share_paths.pop(share_id, None)
        self._phone_share_watch.pop(share_id, None)
        self._transfer_messages.pop(share_id, None)
        row._phone_done = True  # noqa: SLF001 - our own marker
        row.hide()
        self.file_list.clear_all()
        self.file_list.add_paths([path])
        self._update_total()
        self._hand_off_to_phone(peer)
        self.file_list.clear_all()
        self._update_total()
        self.show_page("session", view="chat")

    def _resend_file(self, path: str) -> None:
        """文件动作矩阵里的「重新发送」：放回待发，发给当前选中的设备。"""
        if not path or not os.path.exists(path):
            self.status_left.setText("这个文件已经不在原处了，没法重发。")
            return
        self.file_list.clear_all()
        self.file_list.add_paths([path])
        self._update_total()
        peers = [p for p in self.selected_peers() if peer_is_sendable(p)]
        if not peers:
            self.show_page("session", view="chat")
            self.status_left.setText("已把这个文件放回待发：先在左边选一台设备，再点「发送」。")
            return
        self._send()

    def _restore_received_history(self) -> None:
        """启动时把已经收完的文件恢复到「传输 → 接收」表（G-12）。

        接收行以前只由运行时事件创建（``add_history_row`` 的唯一调用点是
        ``_add_history``），重启后传输页一片空白；而会话库里其实留着每个收到
        文件的记录（``mediaName`` / ``mediaRel`` / 发送者 / 时间）。这里按会话
        最近消息把**文件仍在接收目录**的那些恢复成历史行，行上的「预览 /
        另存为 / 重新发送」才有意义。恢复失败绝不影响窗口启动。
        """
        try:
            receive_dir = os.path.realpath(self.engine.config.receive_dir)
            table = self.transfers_view.recv_table
            seen: set[str] = set()
            restored = 0
            for conversation in self.engine.chat.conversations()[:50]:
                conv_id = str(conversation.get("id") or "")
                if not conv_id:
                    continue
                try:
                    messages = self.engine.chat.messages(conv_id, limit=80)
                except Exception:
                    continue
                for message in reversed(messages):
                    if str(message.get("direction") or "") != "in":
                        continue
                    name = str(message.get("mediaName") or "")
                    rel = str(message.get("mediaRel") or "")
                    if not name and not rel:
                        continue
                    path = os.path.realpath(os.path.join(receive_dir, rel or name))
                    if path in seen or not os.path.isfile(path):
                        continue
                    seen.add(path)
                    timestamp = float(message.get("ts") or 0) or 0.0
                    when = (
                        time.strftime("%H:%M", time.localtime(timestamp))
                        if timestamp
                        else ""
                    )
                    sender = str(message.get("senderName") or "").strip()
                    if not sender or self._looks_like_an_id(sender):
                        sender = self._known_peer_name(conv_id) or "—"
                    try:
                        size_text = human_bytes(os.path.getsize(path))
                    except OSError:
                        size_text = ""
                    key = str(message.get("recordKey") or "") or f"restore:{path}"
                    try:
                        table.add_history_row(
                            name or os.path.basename(path),
                            path,
                            sender,
                            size_text,
                            when,
                            key=key,
                        )
                        restored += 1
                    except Exception:
                        continue
                    if restored >= 200:
                        break
                if restored >= 200:
                    break
            if restored:
                self.transfers_view.refresh_empty()
        except Exception:
            # 恢复是"锦上添花"：数据目录被删/会话库损坏都不能挡住窗口启动。
            pass

    def _add_history(
        self,
        name: str,
        path: str,
        transfer_id: str,
        *,
        peer_name: str = "",
        device_id: str = "",
    ) -> None:
        """收到的文件进「接收」表：一行一个文件，预览/打开/另存为都在那一行上。

        ``peer_name`` / ``device_id`` 给"没有协议传输"的来源用（手机 POST 上来的
        文件只有发件人，没有 transfer_id）。
        """
        try:
            size_text = human_bytes(os.path.getsize(path))
        except OSError:
            size_text = ""
        peer = peer_name or self._recv_from.get(transfer_id, "")
        if not peer:
            row = self._transfers.get(transfer_id)
            peer = getattr(row, "peer_name", "") if row is not None else ""
        if not peer:
            # 短距离的传输在第一次进度轮询之前就传完了，"正在传"那一行根本没建过，
            # 来源就只剩这次传输自己带的那个名字。
            peer = self._transfer_peer_info(transfer_id)[1]
        if not device_id:
            device_id, _name = self._transfer_peer_info(transfer_id)
        size = 0
        try:
            size = os.path.getsize(path)
        except OSError:
            size = 0
        record_key = self._record_key(
            "in",
            device_id or peer,
            self._content_identity(path=path, name=name, size=size),
        )
        self.transfers_view.recv_table.add_history_row(
            name,
            path,
            peer,
            size_text,
            time.strftime("%H:%M", time.localtime()),
            key=record_key,
        )
        self._history_transfers.add(transfer_id)
        # 安全网：结束事件比 file_done 先到时，_finish_row 那一刻还看不到历史行，
        # live 行不会被收起。这里补一刀，保证任何事件顺序下「一个文件只有一行」。
        live = self._transfers.get(transfer_id)
        if (
            live is not None
            and str(getattr(live, "direction", "")) == "recv"
            and getattr(live, "_finished", False)
        ):
            live.hide()
        self._announce_received_file(transfer_id, name, path)
        self.transfers_view.refresh_empty()

    # ------------------------------------------------------------------
    # 传输记录也进会话
    # ------------------------------------------------------------------

    def _transfer_peer_info(self, transfer_id: str) -> tuple[str, str]:
        """这次传输的对端是谁（设备号, 名字）。

        三个来源，按可靠性排：进度轮询里记下的、**此刻还在跑的那次传输自己带着的**
        （``transfer.peer``）、以及收到报价时那条事件里的 ``peer``。短距离的传输
        在第一次进度轮询之前就传完了，只靠轮询会一条都记不到。
        """
        cached = self._transfer_devices.get(transfer_id)
        if cached and cached[0]:
            return cached
        try:
            for transfer in self.engine.active_transfers():
                if transfer.transfer_id != transfer_id:
                    continue
                info = getattr(transfer, "peer", None)
                device_id = str(getattr(info, "device_id", "") or "")
                if device_id:
                    self._transfer_devices[transfer_id] = (device_id, str(getattr(info, "name", "") or ""))
                    return self._transfer_devices[transfer_id]
        except Exception:
            pass
        return ("", "")

    def _peer_by_device_id(self, device_id: str) -> Peer | None:
        """按设备号找这台设备（接收事件里只有来源名字，没有 Peer）。"""
        if not device_id:
            return None
        for table in self.device_tables():
            for peer in table.peers:
                if peer.info.device_id == device_id:
                    return peer
        return None

    def _announce_transfer_message(
        self,
        *,
        conv_id: str,
        direction: str,
        path: str,
        name: str,
        peer: Peer | None,
        state: str,
        rel: str = "",
        sender_name: str = "",
        other_member: str = "",
    ) -> str:
        """给一次传输在会话里留一条记录（有就复用，没有就新建）。

        「传输」页和会话不该是两套账：用户在会话里看到的文件，和他在传输页看到的
        那一行，必须是同一个文件、同一个进度。以前只有聊天里发的附件会变成气泡，
        用「传输文件」按钮发的、以及对方传过来的文件，会话里一个字都没有。
        """
        if not conv_id or not name:
            return ""
        others = [m for m in self._conversation_members(conv_id) if m and m != self.engine.info.device_id]
        if not others and not other_member:
            # 不知道对面是谁就别建会话：一条"只有我自己"的空会话既没法看，
            # 也会把会话列表越堆越乱。
            return ""
        if other_member and other_member not in others:
            others.append(other_member)
        kind = guess_kind(path or name)
        if kind == "folder":
            kind = "file"
        size = 0
        try:
            size = os.path.getsize(path) if path and os.path.exists(path) else 0
        except OSError:
            size = 0
        existing = self._find_transfer_message(conv_id, name, direction, size)
        if existing:
            if state and str(existing.get("state") or "") != state:
                try:
                    self.engine.chat.set_state(str(existing.get("id") or ""), state)
                except Exception:
                    pass
            if rel and str(existing.get("mediaRel") or "") != str(rel):
                # 同一条记录又发布了一次：**引用要换成这一次的**。手机点气泡上的
                # 「保存」时拿的是这条记录里的引用，留着上一份引用就会去下已经
                # 作废的那一份（或者干脆 404）。走账本的合并写：同一 (方向, 对端,
                # 内容) 仍然只有一行，attempts 按"又一次投递"累加（宪章 C8）。
                record_key = str(existing.get("recordKey") or "")
                if record_key:
                    try:
                        self.engine.chat.add_message(
                            conv_id,
                            sender=self.engine.info.device_id
                            if direction == "out"
                            else (peer.info.device_id if peer else ""),
                            sender_name=(
                                self.engine.info.name
                                if direction == "out"
                                else (peer.info.name if peer else sender_name)
                            ),
                            kind=kind,
                            text="",
                            media_name=name,
                            media_rel=rel,
                            media_source=path if direction == "out" else "",
                            media_size=size,
                            direction=direction,
                            state=state or str(existing.get("state") or "sending"),
                            record_key=record_key,
                        )
                        self.chat_view.reload()
                    except Exception:
                        pass
            return str(existing.get("id") or "")
        try:
            self.engine.chat.upsert_conversation(
                conv_id,
                kind="group" if conv_id.startswith("g:") else "direct",
                title=(peer.info.name if peer is not None else ""),
                members=[m for m in (self.engine.info.device_id, *others) if m],
            )
            message = self.engine.chat.add_message(
                conv_id,
                sender=self.engine.info.device_id if direction == "out" else (peer.info.device_id if peer else ""),
                sender_name=(
                    self.engine.info.name
                    if direction == "out"
                    else (peer.info.name if peer else sender_name)
                ),
                kind=kind,
                text="",
                media_name=name,
                media_rel=rel,
                media_source=path if direction == "out" else "",
                media_size=size,
                direction=direction,
                state=state or "done",
            )
        except Exception:
            return ""
        self.chat_view.reload()
        return str(message.get("id") or "")

    def _conversation_members(self, conv_id: str) -> list[str]:
        conversation = self.chat_view.conversation(conv_id) or {}
        members = [str(m) for m in (conversation.get("members") or []) if str(m)]
        if members:
            return members
        try:
            return [str(m) for m in self.engine.chat.members(conv_id) if str(m)]
        except Exception:
            return []

    def _find_transfer_message(self, conv_id: str, name: str, direction: str, size: int) -> dict | None:
        """同一个文件不要重复记：最近 5 分钟内同名同方向同大小就算同一条。"""
        try:
            messages = self.engine.chat.messages(conv_id, limit=60)
        except Exception:
            return None
        now = time.time()
        for message in reversed(messages):
            if str(message.get("direction") or "") != direction:
                continue
            if str(message.get("mediaName") or "") != name:
                continue
            if size and int(message.get("mediaSize") or 0) not in (0, size):
                continue
            if now - float(message.get("ts") or 0) > 300:
                continue
            return message
        return None

    def _announce_received_file(self, transfer_id: str, name: str, path: str) -> None:
        """收到的文件：落到会话里（聊天附件除外 —— 它本来就是一条消息）。"""
        root = os.path.realpath(self.engine.config.receive_dir)
        target = os.path.realpath(path)
        if target.startswith(root + os.sep):
            rel = os.path.relpath(target, root).replace(os.sep, "/")
            # 聊天附件住在 media/<会话>/ 下面，那条消息早就有了，别再记一遍。
            if rel.split("/", 1)[0] == "media":
                return
        else:
            rel = ""
        device_id, sender_name = self._transfer_peer_info(transfer_id)
        found = self._peer_by_device_id(device_id)
        conv_id = self._conversation_for_peer(found) if found is not None else ""
        # 这条兜底必须**先定义**：下面「会话已经存在」时不会进 if 分支，而收尾
        # 还有一处 ``if not other_member`` 要读它。以前只有分支内赋值，于是每收
        # 一个文件就在 Qt 槽里抛 UnboundLocalError（Qt 打印 traceback 后吞掉），
        # 表现是会话里没有文件卡片、页头一直「未知设备 #hash」。
        other_member = ""
        if not conv_id:
            # 还没跟这台设备说过话？那就现开一个会话 —— 传输页一清空就什么都不剩，
            # 会话记录是用户唯一还能翻到"谁给我发过什么"的地方。
            other = found.info.device_id if found is not None else device_id
            other_member = ""
            if other:
                # 用"这台设备在会话里的规范写法"（手机的 App 与浏览器会话都归到
                # web:<设备号>），否则又会多出一段谁也看不到的会话。
                _key, canonical = self._member_device_key(other)
                other_member = canonical or other
                conv_id = direct_conversation_id(self.engine.info.device_id, other_member)
        if not conv_id:
            return
        if not other_member:
            other_member = str(found.info.device_id if found is not None else device_id)
        self._announce_transfer_message(
            conv_id=conv_id,
            direction="in",
            path=path,
            name=name,
            peer=found,
            state="done",
            rel=rel,
            sender_name=sender_name,
            other_member=other_member,
        )

    def _announce_sent_files(self, peer: Peer, paths: list[str], key: str, rel: str = "") -> None:
        """自己用「传输文件」发出去的文件：会话里也要有一行记录 + 进度。

        ``key`` 是这一行的传输号（发送请求发出前是 ``__pending__…``，之后会被
        真正的 transfer id 接管）—— 把消息 id 记在这把钥匙下面，收尾时才找得到
        该把哪条记录改成"已发送"。

        ``rel`` 是这条记录的**可下载引用**：手机是纯 HTTP 客户端，它拿着消息
        里的引用才知道去哪儿取（``share:<分享号>`` 见 ``web/server.py`` 的
        ``/api/chat/media/<消息号>``）。发送方自己本机那份文件仍然记在
        ``mediaSource`` 里，桌面端预览/打开靠它。
        """
        # 还没跟这台设备说过话也要记：用户发的第一个东西可能就是文件，
        # 会话里没有这一笔，传输页一清空就再也看不到"我给谁发过什么"。
        _key, canonical = self._member_device_key(peer.info.device_id)
        conv_id = self._conversation_for_peer(peer) or direct_conversation_id(
            self.engine.info.device_id, canonical or peer.info.device_id
        )
        if not conv_id:
            return
        ids: list[str] = []
        for path in paths:
            if not path or not os.path.exists(path):
                continue
            message_id = self._announce_transfer_message(
                conv_id=conv_id,
                direction="out",
                path=path,
                name=os.path.basename(path),
                peer=peer,
                state="sending",
                rel=rel,
                other_member=self._member_device_key(peer.info.device_id)[1] or peer.info.device_id,
            )
            if message_id:
                ids.append(message_id)
        if ids:
            self._transfer_messages[key] = ids

    def _finish_transfer_messages(self, key: str, ok: bool) -> None:
        """传输结束：把会话里那几条记录的状态改成已发送/失败。"""
        ids = list(self._transfer_messages.pop(key, []))
        for name, mapped in list(self._transfer_messages.items()):
            if name == key:
                continue
        if not ids:
            return
        state = "sent" if ok else "failed"
        changed = False
        for message_id in ids:
            try:
                self.engine.chat.set_state(message_id, state)
                changed = True
            except Exception:
                pass
            if ok:
                # 重试成功了：上一次的失败原因不能再留着，不然气泡上下次一变红
                # 就会出现一句早就不成立的原因。
                self._transfer_failures.pop(str(message_id), None)
        if changed:
            self.chat_view.reload()

    def _fail_transfer_messages(self, key: str, reason: str) -> None:
        """这次传输在会话里那几条记录改成 failed，并把**中文原因**挂上去。

        ``reason`` 会经 ``_transfer_failure_reason`` 交给气泡（store 里没有 reason
        这一列，见 ``ChatView._failed_text``）—— 失败一定有话说，不许静默。
        """
        reason = str(reason or "").strip() or "发送失败（原因不明）"
        for message_id in list(self._transfer_messages.get(key) or []):
            self._transfer_failures[str(message_id)] = reason
        while len(self._transfer_failures) > 200:
            self._transfer_failures.pop(next(iter(self._transfer_failures)), None)
        self._finish_transfer_messages(key, False)
        # 状态可能本来就已经是 failed（重试又失败）：那时 reload() 会因为签名
        # 没变而跳过重画，气泡上还是上一句原因 —— 强制重画一次。
        try:
            self.chat_view.reload_now()
        except Exception:
            pass

    def _transfer_failure_reason(self, message_id: str) -> str:
        """交给 ChatView 的失败原因查询（见 ``set_failure_reason_provider``）。"""
        return self._transfer_failures.get(str(message_id), "")

    def _fail_paths_in_chat(self, peer: Peer, paths: list, reason: str) -> None:
        """没发出去的文件也要在会话里说清楚（读不到的连记录都建不了）。

        以前这里只弹一个对话框：用户点掉之后，会话里没有任何痕迹，"我到底发给
        谁了什么"就彻底没了。现在能建记录的就建一条 failed 的记录（带原因），
        文件本机都不在了就留一句系统说明。
        """
        wanted = [str(p) for p in (paths or []) if str(p or "")]
        if not wanted:
            return
        here = [p for p in wanted if os.path.exists(p)]
        missing = [p for p in wanted if not os.path.exists(p)]
        if here:
            key = f"__failed__{time.time()}"
            self._announce_sent_files(peer, here, key)
            self._fail_transfer_messages(key, reason)
        if missing:
            self._announce_transfer_note(peer, f"没发出去：{reason}")

    def _announce_transfer_note(self, peer: Peer, text: str) -> None:
        """在会话里留一句系统说明（没有对应文件记录时用）。"""
        text = str(text or "").strip()
        if not text:
            return
        _key, canonical = self._member_device_key(peer.info.device_id)
        other = canonical or peer.info.device_id
        conv_id = self._conversation_for_peer(peer) or direct_conversation_id(
            self.engine.info.device_id, other
        )
        if not conv_id:
            return
        try:
            self.engine.chat.upsert_conversation(
                conv_id,
                kind="direct",
                title=peer.info.name,
                members=[m for m in (self.engine.info.device_id, other) if m],
            )
            self.engine.chat.add_message(
                conv_id,
                sender=self.engine.info.device_id,
                sender_name=self.engine.info.name,
                kind="system",
                text=text,
                direction="out",
                state="failed",
            )
        except Exception:
            return
        self.chat_view.reload_now()

    # ------------------------------------------------------------------
    # 同一台手机的几段会话：并回一处
    # ------------------------------------------------------------------

    def offline_device_count(self) -> int:
        """「离线」那一段现在有多少台（设置页要如实报数）。"""
        try:
            return len(self.sidebar.offline_section.peers)
        except Exception:
            return 0

    def _refresh_offline_count(self) -> None:
        label = getattr(self, "offline_count_label", None)
        button = getattr(self, "clear_offline_button", None)
        if label is None:
            return
        remembered = len(self._known_devices)
        total = self.offline_device_count()
        other = max(0, total - remembered)
        label.setText(
            f"离线那一栏现在有 {total} 台：本机记住的 {remembered} 台"
            + (f"，另有 {other} 台来自聊天记录或还没连回来的配对手机。" if other else "。")
            + "\n清空只去掉「以前见过」的名单，聊天记录、配对关系都不动。"
        )
        if button is not None:
            button.setEnabled(remembered > 0 or total > 0)

    def clear_offline_devices(self, ask: bool = True) -> int:
        """清空「以前见过」的离线设备名单（落盘的那份也一起清）。"""
        remembered = len(self._known_devices)
        total = self.offline_device_count()
        if ask and total:
            answer = QMessageBox.question(
                self,
                "清空离线设备列表",
                f"把「离线」那一栏里的设备从列表里去掉？\n\n"
                f"· 本机记住的 {remembered} 台「以前见过」的设备会被清掉\n"
                f"· 聊天记录、文件和配对关系都保留（对方的手机下次连上还在）\n\n"
                f"（这一段现在一共 {total} 台；来自聊天记录的设备要删会话才会消失）",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if answer != QMessageBox.Yes:
                return 0
        removed = len(self._known_devices)
        self._known_devices.clear()
        self._known_dirty = True
        self._save_known_devices(force=True)
        # 有聊天记录的设备行不能跟着清：那种行是用户打开那段会话的唯一入口，
        # 清掉等于把聊天藏起来（对话框里已经说清楚了）。要让它消失就删会话。
        self._refresh_devices()
        self._refresh_offline_count()
        self.status_left.setText(
            f"已清空离线设备列表（去掉 {removed} 台以前见过的设备，聊天记录保留）。"
            if removed
            else "离线列表里没有本机记住的设备。"
        )
        return removed

    # ------------------------------------------------------------------
    # 更强的合并规则（用户确认后才动手）
    # ------------------------------------------------------------------

    def _member_evidence(self, member: str) -> list[str]:
        """这个成员 id 身上有哪些"身份证据"。

        证据分强弱：
          · ``dev:<设备号>`` —— 客户端自己报的设备号（App 的 deviceId、浏览器
            localStorage 里那个 uuid）。**换 IP、换网络都不变**，最强。
          · ``addr:<地址>`` —— 某个已配对客户端现在用的地址。局域网里一个地址就是
            一台设备，够用。
          · ``key:<注册表键>`` —— 注册表里的 地址|UA摘要。
          · ``digest:<摘要>`` —— 只有 UA 摘要（同一款浏览器会撞），最弱：
            只在"用户确认过的整理"里用。
        """
        text = str(member or "")
        mine = self.engine.info.device_id
        if not text or text == mine:
            return []
        clients = self._known_client_records()
        bare = text.split(":", 1)[1] if text.startswith(("web:", "android:")) else text
        evidence: list[str] = []
        address = bare.split("|", 1)[0].split(":", 1)[0]
        digest = bare.split("|", 1)[1] if "|" in bare else (bare.split(":", 1)[1] if bare.startswith(address + ":") else "")
        for client in clients:
            aliases = client_aliases(client)
            if not (text in aliases or bare in aliases):
                continue
            # 这条记录的身份：有设备号就是它（换地址不变）；没有设备号时，如果同地址
            # 同 UA 上有一条**带设备号**的记录，那就是它的"影子记录"（页面首次加载
            # `GET /` 不带自定义头留下的那一条）—— 两者必须算同一台设备，否则同一台
            # 手机的聊天记录会永远散在两段会话里。
            identity = self._client_identity(client, clients)
            if identity:
                evidence.append(identity)
            evidence.append(f"key:{client.get('key')}")
        # 老写法（``web:地址|摘要``、``地址:摘要``）没有直接对上任何记录时：如果
        # 那个地址上有一条**带稳定身份**的记录，而且 **UA 摘要对得上**（或那条记录
        # 压根没有 UA 可对），就认它是这台设备的旧身份。
        #
        # 以前这里只按"地址相同"就发一份 `addr:` 证据 —— 同一台电脑上
        # （127.0.0.1）打开两个不同浏览器的页面时，两段会话会被并查集并成一段
        # （审计员在用真实数据副本上实测到 59→57，并掉的正是两个不同浏览器）。
        # 摘要这一条把"同一台设备的旧写法"和"同一地址上的另一台设备"区分开了。
        if address and digest and not evidence:
            for client in clients:
                if str(client.get("address") or "") != address:
                    continue
                identity = self._client_identity(client, clients)
                if not identity:
                    continue
                agent = str(client.get("agent") or "")
                if agent:
                    fingerprint = hashlib.sha1(
                        agent.encode("utf-8", "replace")
                    ).hexdigest()[:8]
                    if fingerprint != digest:
                        continue
                evidence.append(identity)
                break
        for peer in self.engine.devices():
            if peer.info.device_id == bare:
                evidence.append(f"dev:{bare}")
        if digest:
            evidence.append(f"digest:{digest}")
        if bare and not evidence:
            evidence.append(f"raw:{bare}")
        return evidence

    def plan_conversation_merge(self, strong: bool = False) -> list[dict]:
        """算出"哪些会话该并成一段"，不落库（对话框要先给用户看）。

        ``strong=False``：只按能证明同一台设备的证据（设备号 / 注册表键 / 地址）。
        ``strong=True``：再加上 **UA 摘要** —— 同一款浏览器在不同地址上的记录会被
        认成一台（用户要求的更强规则），代价是"两台一模一样的手机"也可能被并，
        所以只在用户点「整理重复会话」并确认后执行。
        """
        rows = [row for row in self._conversation_rows() if not row.get("is_group")]
        # 并查集：共享任何一条证据的成员算同一台设备
        parent: dict[str, str] = {}

        def find(item: str) -> str:
            parent.setdefault(item, item)
            while parent[item] != item:
                parent[item] = parent[parent[item]]
                item = parent[item]
            return item

        def union(left: str, right: str) -> None:
            root_left, root_right = find(left), find(right)
            if root_left != root_right:
                parent[root_right] = root_left

        def members_of(row: dict) -> list[str]:
            """这一段会话里的"对方"。（可能不止一个：App 与浏览器各写过一次。）"""
            mine = self.engine.info.device_id
            return [
                str(m)
                for m in (row.get("members") or [])
                if str(m) and str(m) != mine
            ]

        member_evidence: dict[str, list[str]] = {}
        for row in rows:
            for member in members_of(row):
                evidence = self._member_evidence(member)
                member_evidence[member] = evidence
                for item in evidence:
                    if item.startswith("digest:") and not strong:
                        continue
                    union(member, item)
        # 一段会话里出现多个"对方"写法时，它们其实是同一台设备的前后两种身份 ——
        # 把这几条证据链**接起来**，否则两段本应合并的会话会因为各自认了不同的
        # 成员而分在两个组里（用户的 .152 那段就是这样漏掉的）。
        bridged: set[str] = set()
        for row in rows:
            others = members_of(row)
            for member in others[1:]:
                union(others[0], member)
                # 记下"这几段是靠'同一条会话里出现过两种写法'接起来的"，好让确认框
                # 如实说明依据 —— 以前这种组会打印一句凭空的「依据：同一台设备」。
                bridged.add(find(others[0]))

        groups: dict[str, list[dict]] = {}
        for row in rows:
            others = members_of(row)
            if not others:
                continue
            member = others[0]
            root = find(member)
            groups.setdefault(root, []).append({
                "member": member,
                "row": row,
                "evidence": [e for e in member_evidence.get(member, []) if strong or not e.startswith("digest:")],
            })
        plans: list[dict] = []
        for root, items in groups.items():
            if len(items) < 2:
                continue
            def weight(entry: dict) -> tuple[int, float]:
                try:
                    count = len(self.engine.chat.messages(str(entry["row"]["id"]), limit=1000))
                except Exception:
                    count = 0
                return (count, -float(entry["row"].get("created") or 0))

            items.sort(key=weight, reverse=True)
            evidence = sorted(
                {e for item in items for e in item["evidence"] if not e.startswith("raw:")}
            )
            if not evidence and find(items[0]["member"]) in bridged:
                # 依据就是"同一条会话里出现过这两种写法"（发送方自己这么报的）。
                evidence = ["同一条会话里的两种写法"]
            plans.append({
                "evidence": evidence,
                "keep": items[0]["row"],
                "drop": [item["row"] for item in items[1:]],
                "members": [item["member"] for item in items],
            })
        return plans

    def fold_conversations(self, strong: bool = False, ask: bool = False) -> int:
        """按计划把重复会话并回一处（``ask=True`` 时先让用户过一眼）。"""
        plans = self.plan_conversation_merge(strong=strong)
        if not plans:
            if ask:
                QMessageBox.information(
                    self, "没有需要整理的会话",
                    "现在没有发现同一台设备散成几段的一对一会话。",
                )
            return 0
        if ask:
            lines = []
            for plan in plans:
                keep_title = str(plan["keep"].get("title") or plan["keep"]["id"])[:28]
                try:
                    keep_count = len(self.engine.chat.messages(str(plan["keep"]["id"]), limit=1000))
                except Exception:
                    keep_count = 0
                dropped = []
                for row in plan["drop"]:
                    try:
                        count = len(self.engine.chat.messages(str(row["id"]), limit=1000))
                    except Exception:
                        count = 0
                    dropped.append(f"{str(row.get('title') or row['id'])[:20]}（{count} 条）")
                lines.append(
                    f"· 保留「{keep_title}」（{keep_count} 条）\n"
                    f"  并掉：{('、'.join(dropped)) or '（空）'}\n"
                    f"  依据：{('、'.join(plan['evidence'])) or '同一台设备'}"
                )
            answer = QMessageBox.question(
                self,
                "整理重复会话",
                "同一台设备因为换地址、换写法散成了几段，并回一处？\n\n"
                + "\n".join(lines)
                + "\n\n消息一条都不会丢；这条规则会按浏览器指纹认设备，"
                "如果你有两台完全一样的手机，它们也可能被并到一起。",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if answer != QMessageBox.Yes:
                return 0
        merged = 0
        emptied = 0
        moved_total = 0
        # 打开着的那段如果被并掉了，消息都搬去了保留的那段：跟着换过去，
        # 否则主区会停在一个不存在的会话上（顶上空状态、下面留着旧气泡）。
        open_now = self.chat_view.current_conversation()
        reopen = ""
        for plan in plans:
            keep_id = str(plan["keep"]["id"])
            drop_ids = [str(row["id"]) for row in plan["drop"]]
            try:
                moved = int(self.engine.chat.merge_conversations(keep_id, drop_ids) or 0)
            except Exception:
                continue
            moved_total += moved
            if moved:
                # 真的搬动了消息才算"整理了一组"：一组里只有一段空壳会话时，
                # 写「整理了 1 组（合并 0 条消息）」是在骗用户。
                merged += 1
            else:
                emptied += 1
            if open_now and open_now in drop_ids:
                reopen = keep_id
        if merged or emptied:
            self.chat_view.reload()
            if reopen:
                # 整理后把"被并掉的当前会话"改开成保留那段：同样是程序替用户改开，
                # 消息搬过去了但用户没点，不标已读。
                self.chat_view.open_conversation(reopen, mark_read=False)
            self._refresh_devices()
            self.status_left.setText(_merge_status_text(merged, emptied, moved_total))
        return merged + emptied

    def _known_client_records(self) -> list[dict]:
        ui = getattr(self, "_web_ui", None)
        if ui is None:
            return []
        try:
            return list(ui.known_clients())
        except Exception:
            return []

    def _member_device_key(self, member: str) -> tuple[str, str]:
        """把一个成员 id 归一成"哪台设备"：(归一键, 该用的规范写法)。

        只认**能证明是同一台设备**的情形：
          · 成员 id 本身就是某个已配对客户端的写法（deviceId / key / 各种前缀）；
          · 成员 id 是"地址|摘要"这种，而那个地址上**只有一个 App 客户端** ——
            同一台手机的 App 和它的浏览器会话算一台设备；
          · 协议设备（device_id 稳定）直接用设备号。
        同一地址上有多台设备（NAT 后面）时**不并**，免得把两台手机并成一台。
        """
        text = str(member or "")
        mine = self.engine.info.device_id
        if not text or text == mine:
            return ("", "")
        clients = self._known_client_records()
        bare = text.split(":", 1)[1] if text.startswith(("web:", "android:")) else text
        for client in clients:
            aliases = client_aliases(client)
            if text in aliases or bare in aliases:
                device_id = str(client.get("deviceId") or "")
                if device_id:
                    return (f"dev:{device_id}", f"web:{device_id}")
                return (f"key:{client.get('key')}", text)
        address = bare.split("|", 1)[0].split(":", 1)[0]
        at_address = [
            c for c in clients if str(c.get("address") or "") == address and str(c.get("kind") or "") == "app"
        ]
        if len(at_address) == 1:
            device_id = str(at_address[0].get("deviceId") or "")
            if device_id:
                return (f"dev:{device_id}", f"web:{device_id}")
        for peer in self.engine.devices():
            if peer.info.device_id == bare:
                return (f"dev:{bare}", bare)
        return (f"raw:{bare}", text)

    def _fold_split_conversations(self) -> int:
        """启动时的自动整理：只用"能证明是同一台设备"的证据（弱规则）。"""
        try:
            return self.fold_conversations(strong=False, ask=False)
        except Exception:
            return 0

    def preview_path(self, path: str, name: str = "") -> None:
        """看一眼文件内容（图片/文本/视频），不打开别的程序。

        会话气泡上的「预览」、待发方块、接收表里的「预览」都走这一条路 ——
        三个地方看到的东西必须一样。
        """
        if not path or not os.path.exists(path):
            self.status_left.setText("这个文件不在这台机器上，没法预览。")
            return
        from .preview import FilePreviewDialog

        FilePreviewDialog(
            path, name or os.path.basename(path), self, self.engine.config.data_dir
        ).exec()

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

    # ------------------------------------------------------------------
    # browser UI + appearance
    # ------------------------------------------------------------------

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

            # 同一台手机可能同时留下两条记录（带设备号的 + 页面首次加载那条）：
            # 先归并再数，不然这句话会把一台手机说成两台。
            phones = self._merge_client_records(
                [c for c in ui.known_clients() if is_mobile_client(c)]
            )
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

    def _apply_theme_choice(self) -> None:
        """Switch between the dark and light palette without a restart."""
        choice = str(self.theme_combo.currentData() or "dark")
        os.environ["EVERSEND_THEME"] = choice
        # 样式表换掉之后，自己画的那些控件不会自动重绘：图标是按当前配色
        # 画出来的位图，得重画一遍（这一步在 _restyle_app 里）。
        self._restyle_app()
        self.status_left.setText("主题已切换为" + ("深色" if choice == "dark" else "浅色"))
        self._save_settings(silent=True)

    def _apply_font_choice(self) -> None:
        """字号档变了：立刻重新套用字体与样式表，并写回设置。"""
        choice = str(self.font_combo.currentData() or "standard")
        theme.set_scale(choice)
        self._restyle_app()
        self.status_left.setText(
            "字号已切换为" + theme.SCALE_LABELS.get(choice, choice) + "（立刻生效）"
        )
        self._save_settings(silent=True)

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
        if self.auto_radio_all.isChecked():
            config.auto_accept_all = True
            config.auto_accept_trusted = True
        elif self.auto_radio_trusted.isChecked():
            config.auto_accept_all = False
            config.auto_accept_trusted = True
        else:
            config.auto_accept_all = False
            config.auto_accept_trusted = False
        config.resume = self.resume_box.isChecked()
        config.enable_broadcast = self.broadcast_box.isChecked()
        config.enable_mdns = self.mdns_box.isChecked()
        theme_choice = str(self.theme_combo.currentData() or "dark")
        # 记录保留策略（宪章 C7）：改了立刻修剪一次，不然用户要等下一次写入。
        keep_records = int(self.keep_count_spin.value())
        keep_days = int(self.keep_days_spin.value())
        try:
            self.engine.chat.keep_records = keep_records
            self.engine.chat.keep_days = keep_days
            self.engine.chat.prune_records(keep=keep_records, days=keep_days)
        except Exception:
            keep_records = int(getattr(self.engine.chat, "keep_records", 500))
            keep_days = int(getattr(self.engine.chat, "keep_days", 30))

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
                    "theme": theme_choice,
                    "font_scale": theme.current_scale_name(),
                    "enableBroadcast": config.enable_broadcast,
                    "enableMdns": config.enable_mdns,
                    "keepRecords": keep_records,
                    "keepDays": keep_days,
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
        self._refresh_diagnosis()
        self.engine.announce()
        if not silent:
            self.status_left.setText("设置已保存并生效")

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt naming
        try:
            self._save_settings(silent=True)
        except Exception:
            pass
        try:
            self._known_dirty = True
            self._save_known_devices(force=True)
        except Exception:
            pass
        for dialog in list(self._offer_dialogs.values()):
            try:
                dialog.reject()
            except Exception:
                pass
        self.bridge.close()
        super().closeEvent(event)


def _files_summary(names: list[str]) -> str:
    """One line naming what a transfer is moving (a send can carry many files)."""
    if len(names) == 1:
        return names[0]
    return f"{names[0]} 等 {len(names)} 项"


def _merge_status_text(merged: int, emptied: int, moved: int) -> str:
    """整理完重复会话后，状态行该怎么说。

    只对**真的搬动了消息**的分组说"整理了 N 组"，只清掉空壳的那些如实写成
    "清掉了 N 段空会话" —— 合并 0 条消息却报「整理了 1 组重复会话（合并 0 条
    消息）」，用户看到只会以为程序在说梦话。
    """
    parts: list[str] = []
    if merged:
        parts.append(f"整理了 {merged} 组重复会话（合并 {moved} 条消息）")
    if emptied:
        parts.append(f"清掉了 {emptied} 段空会话（里面没有消息）")
    return "，".join(parts) + "，消息一条没丢。"


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
    if "enableBroadcast" in settings:
        config.enable_broadcast = bool(settings["enableBroadcast"])
    if "enableMdns" in settings:
        config.enable_mdns = bool(settings["enableMdns"])
    # 主题偏好不是引擎配置，而是整个进程的界面选择：把它塞进环境变量，
    # theme.is_dark() 一读就知道（显式设了 EVERSEND_THEME 的人仍然优先）。
    choice = str(settings.get("theme") or "").strip().lower()
    if choice in ("dark", "light"):
        os.environ.setdefault("EVERSEND_THEME", choice)
    # 字号档（宪章 §3.4 外观）：这里定下来，app.py 后面那次
    # ``theme.application_font()`` 就会带着它 —— 第一帧的字号就是对的，
    # 不会先按标准画一遍再跳一下。
    scale = str(settings.get("font_scale") or settings.get("fontScale") or "").strip()
    if scale:
        try:
            theme.set_scale(scale)
        except Exception:
            pass


__all__ = ["MainWindow", "apply_settings", "load_settings"]
