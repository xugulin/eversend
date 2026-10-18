"""传输页：一行一次投递，筛选药丸 + 搜索 + 可排序表头 + 空状态。

宪章 ``docs/ENTERPRISE_CHARTER.md`` §3.2 与 §3.1 的表格决定了这一页的样子：

* 顶部一行：``[全部｜进行中｜已完成｜失败]`` 药丸 + 搜索框 + 「打开接收文件夹」主按钮
  + ``⋯``（``⋯`` 里是「更改接收位置…」和「清空记录…」）—— 这三个以前是三个平级按钮，
  用户的原话就是"有功能重复的按钮，排版丑陋"；
* ``[接收｜发送]`` 就地换表：两张表共用同一块区域，不跳页、不弹窗；
* 行高 44、第一列前面是类型图标、状态是药丸、发送方向的进度条长在「进度」列里；
* 表头可排序（文件 / 大小 / 时间 / 状态）；空表给空状态（图标 + 一句话 + 主按钮）；
* 失败行写**中文原因** + 行内「重试」。

**为什么不是 QTableWidget**：宪章 E5 要求 1000 条记录滚动流畅。``QTableWidget``
每格一个 item、每行还要塞 cell widget，1000 行就是几千个 QObject；这里改成
``QAbstractTableModel`` 持有数据 + 一个 ``QStyledItemDelegate`` 画进度条/药丸/操作文字，
**视图里没有一行拥有自己的控件**：行数只是数据量，不是控件量。

对外仍然保留 ``TransferRow`` 这套行句柄（``.title/.status/.detail/.bar/.cancel_button``），
窗口和测试一直是按它写的；只是它现在改的是模型里的数据，而不是某个 widget 的文本。
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QObject, QRect, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPalette, QPen
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QPushButton,
    QSizePolicy,
    QStackedWidget,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from ..core.model import human_bytes, human_duration, human_speed
from . import icons, theme
from .widgets import SegmentedControl, file_actions_menu, icon_button

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

#: 宪章 §3.2 的列
SEND_COLUMNS = ("文件", "发给", "大小", "进度", "速度", "剩余", "状态", "操作")
#: 接收方向同样要给出「进度 / 速度 / 剩余」：网页端与手机端一直有，桌面端只有
#: 状态徽标时，用户完全不知道还要等多久（手机→电脑是最常用的方向）。
RECV_COLUMNS = ("文件", "来自", "大小", "进度", "速度", "剩余", "时间", "状态", "操作")

#: 筛选药丸（宪章 §3.2 顶部一行）
FILTERS = (("all", "全部"), ("active", "进行中"), ("done", "已完成"), ("failed", "失败"))

#: 传输通道。**两种通道的"进度"来源完全不同，不能混着说**：
#: * ``peer``：协议设备（电脑↔电脑），进度来自本机 TCP 发出去/收回来的字节；
#: * ``phone``：手机是纯 HTTP 客户端（App 进程没有 LISTEN 端口，推不过去），
#:   本机只能把文件**发布**出去、等它来取 —— 进度 = 服务端已经发出去的字节数。
#:   所以"手机取走"之前，这一行**不许**显示完成（见 docs/REAL_DEVICE_FINDINGS.md §2.4）。
CHANNEL_PEER = "peer"
CHANNEL_PHONE = "phone"

#: 手机交接的四个阶段（唯一的事实来源；状态文案与进度条都从这里推）。
PHONE_WAITING = "waiting"
PHONE_OFFLINE = "offline"
PHONE_DOWNLOADING = "downloading"
PHONE_TAKEN = "taken"
PHONE_FAILED = "failed"

#: 状态分类 —— 药丸按它分组，颜色也按它取。
KIND_ACTIVE = "active"
KIND_DONE = "done"
KIND_FAILED = "failed"
KIND_CANCELLED = "cancelled"

#: 状态分类 → theme.STATUS_COLORS 的语义名。
STATUS_STATE = {
    KIND_ACTIVE: "info",
    KIND_DONE: "success",
    KIND_FAILED: "danger",
    KIND_CANCELLED: "warning",
}

#: 排序时"哪种状态排在前面"（进行中的最要紧）。
STATUS_RANK = {KIND_ACTIVE: 0, KIND_FAILED: 1, KIND_CANCELLED: 2, KIND_DONE: 3}

#: 模型里的自定义 role。
ROLE_ITEM = int(Qt.UserRole) + 1
ROLE_SORT = int(Qt.UserRole) + 2
ROLE_PROGRESS = int(Qt.UserRole) + 3

#: 操作格里 ``⋯`` 的点击宽度（右边的窄条）。
OVERFLOW_WIDTH = 28

#: 宪章 §2.1 的语义颜色。``theme.py`` 由 Lead 独占，他导出同名常量之后这里自动改用它；
#: 在那之前用宪章里那组值兜底（值是一样的，只是少了单一来源）。
_FALLBACK_COLORS = {
    True: {
        "canvas": "#0f0f11",
        "surface": "#151517",
        "raised": "#1b1b1f",
        "overlay": "#232329",
        "border": "#2a2a30",
        "border_strong": "#3a3a42",
        "text": "#f4f4f5",
        "text_muted": "#a1a1aa",
        "text_faint": "#71717a",
    },
    False: {
        "canvas": "#ffffff",
        "surface": "#f7f7f8",
        "raised": "#ffffff",
        "overlay": "#ffffff",
        "border": "#e4e4e7",
        "border_strong": "#d4d4d8",
        "text": "#18181b",
        "text_muted": "#52525b",
        "text_faint": "#71717a",
    },
}
_COLOR_CACHE: dict[bool, dict[str, str]] = {}


def colors() -> dict[str, str]:
    """宪章 §2.1 的一组语义色（随深浅主题变，切换主题后立刻跟着变）。

    ``theme.colors()`` 是唯一来源；万一它还没导出（Lead 独占 theme.py），
    这里退回宪章里的同一组值，保证单独跑这个文件也不会炸。
    """
    dark = theme.is_dark()
    cached = _COLOR_CACHE.get(dark)
    if cached is not None:
        return cached
    provider = getattr(theme, "colors", None)
    if callable(provider):
        try:
            base = {str(key): str(value) for key, value in provider(dark).items()}
        except Exception:  # pragma: no cover - 兜底
            base = dict(_FALLBACK_COLORS[dark])
    else:  # pragma: no cover - 兜底
        base = dict(_FALLBACK_COLORS[dark])
    for name, value in _FALLBACK_COLORS[dark].items():
        base.setdefault(name, value)
    _COLOR_CACHE.clear()
    _COLOR_CACHE[dark] = base
    return base


# ---------------------------------------------------------------------------
# 数据
# ---------------------------------------------------------------------------


@dataclass
class TransferItem:
    """传输表里的一行（一次投递）。

    字段就是宪章 §3.2 要求"每一行说清"的那几件事：文件、对端、大小、时间、
    进度、速度、剩余、结果（状态 + 失败原因）。``kind`` 是给筛选药丸用的分类，
    刻意存成字段而不是每次去猜状态文本 —— 中文文案以后改了，筛选不会跟着坏。
    """

    key: str
    direction: str = "send"
    name: str = ""
    peer: str = ""
    size_bytes: int = 0
    size_text: str = ""
    when: str = ""
    when_ts: float = 0.0
    progress: float = 0.0
    indeterminate: bool = False
    speed_bps: float = 0.0
    eta: float | None = None
    status: str = "准备中…"
    kind: str = KIND_ACTIVE
    reason: str = ""
    path: str = ""
    paths: list = field(default_factory=list)
    hidden: bool = False
    transfer_id: str = ""
    #: 传输通道（``peer`` / ``phone``）与手机交接阶段，见模块顶部的说明。
    channel: str = CHANNEL_PEER
    phone_phase: str = ""
    #: 服务端已发出的字节 / 这次要发的总字节（手机通道的进度就是这两个数）。
    sent_bytes: int = 0
    total_bytes: int = 0
    #: 同一个 (方向, 对端, 内容) 被投递了几次（宪章 C8：重复投递合并成一行并累加）。
    attempts: int = 1
    action_override: str = ""
    action_enabled: bool = True
    show_progress: bool = True

    # -- 派生 ---------------------------------------------------------------

    def is_active(self) -> bool:
        return self.kind == KIND_ACTIVE

    def retryable(self) -> bool:
        """这一行能不能「重试」。

        失败/取消的行**永远给这个入口**（宪章 §3.2：失败行行内给「重试」）——
        源文件可能已经被移走，那种情况下窗口会如实说"文件不在了，没法重发"，
        比一个消失的按钮好：用户的第一反应都是"再试一次"。
        """
        return self.kind in (KIND_FAILED, KIND_CANCELLED)

    def action_text(self) -> str:
        """操作格里写什么（宪章 §3.2 的「操作」列）。"""
        if self.action_override:
            return self.action_override
        if self.is_active():
            return "取消"
        if self.path:
            return "预览"
        if self.retryable():
            return "重试"
        return "移除"

    def has_overflow(self) -> bool:
        """这一行的操作格右边有没有 ``⋯``（文件动作矩阵从那里进）。"""
        return bool(self.path)

    def is_phone(self) -> bool:
        return self.channel == CHANNEL_PHONE

    def peer_display(self) -> str:
        """「发给 / 来自」那一列：手机要一眼看得出来（宪章 C4/E1）。"""
        if not self.peer or self.peer == "—":
            return self.peer or "—"
        if self.is_phone() and "手机" not in self.peer:
            return f"{self.peer}（手机）"
        return self.peer

    def percent(self) -> float:
        """真实百分比（0..100）。总量未知时返回 0。"""
        if self.phone_phase == PHONE_DOWNLOADING or self.channel == CHANNEL_PHONE:
            if self.total_bytes > 0:
                return max(0.0, min(100.0, 100.0 * self.sent_bytes / self.total_bytes))
        return max(0.0, min(100.0, 100.0 * self.progress))

    def progress_text(self) -> str:
        # 已经结束的行一律 100%：历史行（收完的文件）没有进度事件，字段默认 0，
        # 以前会显示成「0.0%」—— 一个已完成的文件写着 0% 比不显示更糟。
        if self.kind == KIND_DONE and not self.is_phone():
            return "100%"
        if self.is_phone():
            if self.phone_phase == PHONE_TAKEN:
                return "100%"
            if self.phone_phase in (PHONE_WAITING, PHONE_OFFLINE):
                return "等它下载"
            if self.indeterminate:
                return "手机正在取…"
        if self.indeterminate:
            return "等待对方…"
        if self.is_phone() and self.total_bytes > 0:
            return f"{self.percent():.0f}%"
        # 小数位与网页端一致（app.js 的 percent.toFixed(0)）：同一次传输两端都
        # 写「63%」，不会一边 62.5% 一边 63%。
        return f"{100.0 * max(0.0, min(1.0, self.progress)):.0f}%"

    def phone_total_text(self) -> str:
        """手机行「大小」列：已发出 / 总量。"""
        if self.total_bytes <= 0:
            return self.size_text
        return f"{human_bytes(self.sent_bytes)} / {human_bytes(self.total_bytes)}"

    def speed_text(self) -> str:
        return human_speed(self.speed_bps) if self.speed_bps > 0 else ""

    def eta_text(self) -> str:
        if self.eta is None or self.eta <= 0:
            return ""
        return "剩余 " + human_duration(self.eta)

    def tooltip(self) -> str:
        lines = [self.name or "（没有名字）"]
        if self.peer:
            lines.append(("发给：" if self.direction == "send" else "来自：") + self.peer)
        detail = " · ".join(part for part in (self.size_text, self.when, self.status) if part)
        if detail:
            lines.append(detail)
        if self.attempts > 1:
            lines.append(f"这个文件投递过 {self.attempts} 次（合并成一行显示）。")
        if self.reason:
            lines.append("原因：" + self.reason)
        if self.path:
            lines.append(self.path)
        if self.kind == KIND_ACTIVE:
            lines.append("正在传输，点「取消」可以停下。" if self.direction == "send" else "正在接收，点「取消」可以停下。")
        return "\n".join(lines)


def _kind_for_status(text: str, ok: bool | None = None) -> str:
    """状态文字 → 筛选分类。只在 status 那一处做这个判断。"""
    if ok is True:
        return KIND_DONE
    if ok is False:
        return KIND_FAILED
    if "取消" in text:
        return KIND_CANCELLED
    if "失败" in text or "出错" in text or "拒绝" in text:
        return KIND_FAILED
    if "完成" in text or "已发送" in text or "已取走" in text:
        return KIND_DONE
    return KIND_ACTIVE


class TransferModel(QAbstractTableModel):
    """传输数据。**没有 widget**：视图按需问它要文字/图标/进度。

    ``items`` 是全部数据（插入顺序，行句柄按它定位）；``rows`` 是当前筛选 +
    搜索之后真正显示的那些。排序只改 ``items`` 的顺序，不复制数据。
    """

    COLUMN_KEYS = {
        "recv": {"文件": "name", "来自": "peer", "大小": "size_bytes", "时间": "when_ts",
                 "状态": "status", "操作": "action"},
        "send": {"文件": "name", "发给": "peer", "大小": "size_bytes", "进度": "progress",
                 "速度": "speed_bps", "剩余": "eta", "状态": "status", "操作": "action"},
    }

    def __init__(self, direction: str, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.direction = direction
        self.columns = SEND_COLUMNS if direction == "send" else RECV_COLUMNS
        self.items: list[TransferItem] = []
        self.rows: list[TransferItem] = []
        #: 稳定键 -> 那一行。**键是 (方向, 对端, 内容) 的摘要**（宪章 C8），
        #: 不是文件名、不是传输号：同一次投递的"进行中"和"已完成"、以及重复
        #: 投递同一个文件，都必须落到同一行。
        self.by_key: dict[str, TransferItem] = {}
        self.filter_key = "all"
        self.search_text = ""
        self._sort_column = -1
        self._sort_order = Qt.AscendingOrder

    # -- Qt 接口 -----------------------------------------------------------

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802 - Qt naming
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self.columns)

    def headerData(self, section: int, orientation, role=Qt.DisplayRole):  # noqa: N802
        if orientation != Qt.Horizontal:
            return None
        if role == Qt.DisplayRole and 0 <= section < len(self.columns):
            return self.columns[section]
        if role == Qt.TextAlignmentRole:
            return int(Qt.AlignLeft | Qt.AlignVCenter) if section == 0 else int(Qt.AlignCenter)
        return None

    def data(self, index: QModelIndex, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        item = self.rows[index.row()]
        column = index.column()
        if role == ROLE_ITEM:
            return item
        if role == Qt.DisplayRole:
            return self.column_text(item, column)
        if role == Qt.DecorationRole and column == 0:
            return self._icon(item)
        if role == Qt.ToolTipRole:
            return item.tooltip()
        if role == ROLE_PROGRESS:
            return item.progress
        if role == ROLE_SORT:
            return self.sort_key(item, column)
        if role == Qt.TextAlignmentRole:
            return int(Qt.AlignLeft | Qt.AlignVCenter) if column == 0 else int(Qt.AlignCenter)
        if role == Qt.ForegroundRole and self.column_name(column) == "状态":
            return QColor(theme.STATUS_COLORS.get(STATUS_STATE.get(item.kind, ""), theme.STATUS_COLORS["offline"]))
        return None

    def flags(self, index: QModelIndex):
        return Qt.ItemIsEnabled | Qt.ItemIsSelectable

    def sort(self, column: int, order=Qt.AscendingOrder) -> None:  # noqa: N802
        """表头点击（宪章 §3.2：文件名/大小/时间/状态可排序）。

        ``column < 0`` 表示"取消排序"，回到插入顺序 —— 传输表默认就该按时间排，
        不该因为 QTableView 打开排序就自己按文件名重排一次。
        """
        self._sort_column = int(column)
        self._sort_order = order
        if column < 0:
            self._apply_sort()
        else:
            self.layoutAboutToBeChanged.emit()
            self._apply_sort()
            self.layoutChanged.emit()

    def _apply_sort(self) -> None:
        if self._sort_column < 0:
            return
        reverse = self._sort_order == Qt.DescendingOrder
        name = self.column_name(self._sort_column)

        def key(item: TransferItem):
            value = self.sort_key(item, self._sort_column)
            return (value is None, value)

        try:
            self.items.sort(key=key, reverse=reverse)
        except TypeError:  # pragma: no cover - 混合类型时按文字兜底
            self.items.sort(key=lambda item: str(self.column_text(item, self._sort_column)), reverse=reverse)
        self._rebuild_rows()

    def column_name(self, column: int) -> str:
        if 0 <= column < len(self.columns):
            return self.columns[column]
        return ""

    def column_text(self, item: TransferItem, column: int) -> str:
        name = self.column_name(column)
        return {
            "文件": item.name,
            "发给": item.peer_display(),
            "来自": item.peer_display(),
            "大小": item.phone_total_text() if item.is_phone() else item.size_text,
            "时间": item.when,
            "进度": item.progress_text(),
            "速度": item.speed_text(),
            "剩余": item.eta_text(),
            "状态": item.status,
            "操作": item.action_text(),
        }.get(name, "")

    def sort_key(self, item: TransferItem, column: int):
        name = self.column_name(column)
        if name == "文件":
            return (item.name or "").casefold()
        if name == "大小":
            return item.size_bytes
        if name in ("发给", "来自"):
            return (item.peer or "").casefold()
        if name == "时间":
            return item.when_ts
        if name == "进度":
            return item.progress
        if name == "速度":
            return item.speed_bps
        if name == "剩余":
            return item.eta if item.eta is not None else 1e12
        if name == "状态":
            return (STATUS_RANK.get(item.kind, 9), (item.status or "").casefold())
        return ""

    def _icon(self, item: TransferItem):
        kind = "file"
        name = (item.name or "").lower()
        if item.path:
            try:
                from .chat_view import guess_kind

                kind = guess_kind(item.path or item.name)
            except Exception:
                kind = "file"
        else:
            for suffix, guess in (
                ((".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"), "image"),
                ((".mp4", ".mkv", ".mov", ".avi", ".webm"), "film"),
                ((".mp3", ".wav", ".ogg", ".m4a", ".flac"), "music"),
                ((".zip", ".tar", ".gz", ".7z", ".rar", ".xz"), "archive"),
                ((".py", ".js", ".ts", ".json", ".md", ".txt", ".sh", ".toml", ".yaml", ".yml"), "code"),
            ):
                if name.endswith(suffix):
                    kind = guess
                    break
        # 宪章 §2.3：类型图标 20×20
        icon = icons.icon(kind, colors()["text_muted"], 20)
        return icon

    # -- 数据操作 -----------------------------------------------------------

    def add(self, item: TransferItem, key: str = "") -> TransferItem:
        """入库一行；**命中同一个键就复用那一行**，不新增（宪章 C8）。

        返回真正在表里的那个 ``TransferItem`` —— 合并时它是**已有的那一行**，
        调用方（``TransferRow``）必须拿这个返回值继续更新，否则第二次投递会去
        改一个不在表里的对象，界面上看起来就是"没反应"。
        """
        resolved = str(key or item.key or item.transfer_id or "")
        existing = self.by_key.get(resolved) if resolved else None
        if existing is not None and existing is not item:
            existing.attempts = max(existing.attempts, item.attempts) + (1 if item.attempts <= 1 else 0)
            # 最新一次投递的状态说了算（重复发送时那一行应该显示"正在传"），
            # 但已经落盘的历史行不会被"进行中"顶掉（见 add_history_row 的用法）。
            if not existing.path or item.path:
                existing.status = item.status or existing.status
                existing.kind = item.kind
                existing.progress = item.progress
                existing.indeterminate = item.indeterminate
                existing.speed_bps = item.speed_bps
                existing.eta = item.eta
            for path in item.paths:
                if path not in existing.paths:
                    existing.paths.append(path)
            if item.path and not existing.path:
                existing.path = item.path
            if item.reason and not existing.reason:
                existing.reason = item.reason
            if resolved and not existing.key:
                existing.key = resolved
            self.touch(existing)
            return existing
        if resolved:
            self.by_key[resolved] = item
            item.key = resolved
        row = len(self.rows)
        visible = self.accepts(item) and not item.hidden
        self.beginInsertRows(QModelIndex(), row, row)
        self.items.append(item)
        if visible:
            self.rows.append(item)
        self.endInsertRows()
        if self._sort_column >= 0:
            self._apply_sort()
            self.beginResetModel()
            self.endResetModel()
        return item

    def touch(self, item: TransferItem) -> None:
        """一条数据变了：只刷新它那一行，不整表重画。"""
        if item in self.rows:
            row = self.rows.index(item)
            self.dataChanged.emit(self.index(row, 0), self.index(row, len(self.columns) - 1))

    def accepts(self, item: TransferItem) -> bool:
        if self.filter_key == "failed":
            if item.kind not in (KIND_FAILED, KIND_CANCELLED):
                return False
        elif self.filter_key != "all" and item.kind != self.filter_key:
            return False
        query = self.search_text.strip().casefold()
        if query and query not in (item.name or "").casefold() and query not in (item.peer or "").casefold():
            return False
        return True

    def _rebuild_rows(self) -> None:
        self.rows = [item for item in self.items if not item.hidden and self.accepts(item)]

    def refresh(self) -> None:
        self.beginResetModel()
        self._rebuild_rows()
        self.endResetModel()

    def set_filter(self, key: str) -> None:
        self.filter_key = key or "all"
        self.refresh()

    def set_search(self, text: str) -> None:
        self.search_text = text or ""
        self.refresh()

    def set_hidden(self, item: TransferItem, hidden: bool) -> None:
        item.hidden = bool(hidden)
        if item.hidden and item in self.rows:
            row = self.rows.index(item)
            self.beginRemoveRows(QModelIndex(), row, row)
            self.rows.pop(row)
            self.endRemoveRows()
        elif not item.hidden and item not in self.rows and self.accepts(item):
            row = len(self.rows)
            self.beginInsertRows(QModelIndex(), row, row)
            self.rows.append(item)
            self.endInsertRows()

    def row_of(self, item: TransferItem) -> int:
        return self.rows.index(item) if item in self.rows else -1

    def all_visible(self) -> list[TransferItem]:
        return list(self.rows)

    def busy(self) -> int:
        return len([item for item in self.items if item.is_active() and not item.hidden])


# ---------------------------------------------------------------------------
# 画法
# ---------------------------------------------------------------------------


class TransferDelegate(QStyledItemDelegate):
    """进度条 / 状态药丸 / 操作文字都在这里画。

    一行一个控件在 1000 行时会卡；一行一个 delegate 调用不会。
    """

    def __init__(self, table: "TransferTable") -> None:
        super().__init__(table)
        self.table = table

    def sizeHint(self, option, index) -> QSize:  # noqa: N802 - Qt naming
        """列宽就是按这个算的（其余列是 ``ResizeToContents``）。

        自绘的三列（进度/状态/操作）给固定宽度 —— 画的药丸和文字，Qt 量不出来；
        文本列按文字量，但封顶 240px，别让一个长文件名把整张表撑出横向滚动条。
        """
        height = theme.ROW_HEIGHT
        model = self.table.model()
        name = model.column_name(index.column()) if index.isValid() else ""
        if name == "进度":
            return QSize(168, height)
        if name in ("状态", "操作"):
            return QSize(96, height)
        base = super().sizeHint(option, index)
        return QSize(max(60, min(int(base.width()) + theme.SPACE["sm"], 240)), height)

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex) -> None:
        item = index.data(ROLE_ITEM)
        if not isinstance(item, TransferItem):
            super().paint(painter, option, index)
            return
        name = self.table.model().column_name(index.column())
        painter.save()
        if name == "文件":
            self._paint_name(painter, option, index, item)
        elif name == "进度" and item.direction == "send":
            self._paint_progress(painter, option, item)
        elif name == "状态":
            self._paint_pill(painter, option, item)
        elif name == "操作":
            self._paint_action(painter, option, item)
        else:
            super().paint(painter, option, index)
        painter.restore()

    # -- 背景 ---------------------------------------------------------------

    def _fill_background(self, painter: QPainter, option: QStyleOptionViewItem) -> None:
        if option.state & QStyle.State_Selected:
            painter.fillRect(option.rect, option.palette.color(QPalette.Highlight))
        elif option.state & QStyle.State_MouseOver:
            colour = QColor(option.palette.color(QPalette.Text))
            colour.setAlpha(14)
            painter.fillRect(option.rect, colour)

    # -- 各列 ---------------------------------------------------------------

    def _paint_progress(self, painter: QPainter, option: QStyleOptionViewItem, item: TransferItem) -> None:
        """进度条画在「进度」列里：一条细槽 + 一块填充 + 居中的百分比。"""
        self._fill_background(painter, option)
        palette = colors()
        # 竖直方向留出 SPACE["sm"]，条高固定在 18（和 CONTROL["sm"]/2 一个量级），
        # 不再撑满整格 —— 以前那个黑块就是"填充色取错 + 高度撑满"两件事凑出来的。
        height = 18
        inner = QRect(option.rect)
        inner.setLeft(inner.left() + theme.SPACE["md"])
        inner.setRight(inner.right() - theme.SPACE["md"])
        inner.setTop(inner.center().y() - height // 2)
        inner.setHeight(height)
        if inner.width() <= 8:
            return
        radius = theme.RADIUS["control"]
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(palette["progress_track"]))
        painter.drawRoundedRect(inner, radius, radius)
        if item.indeterminate or not item.show_progress:
            ratio = 0.3
        else:
            ratio = max(0.0, min(1.0, item.progress))
        filled = QRect(inner)
        filled.setWidth(int(inner.width() * ratio))
        if filled.width() > 0:
            painter.setBrush(QColor(palette["progress_chunk"]))
            painter.drawRoundedRect(filled, radius, radius)
        font = QFont(painter.font())
        size, weight = theme.font("badge")
        font.setPixelSize(size)
        font.setWeight(QFont.Weight(weight))
        painter.setFont(font)
        painter.setPen(QColor(palette["text"]))
        painter.drawText(inner, Qt.AlignCenter, item.progress_text() if item.show_progress else "")

    def _paint_pill(self, painter: QPainter, option: QStyleOptionViewItem, item: TransferItem) -> None:
        self._fill_background(painter, option)
        state = STATUS_STATE.get(item.kind, "offline")
        colour = QColor(theme.STATUS_COLORS.get(state, theme.STATUS_COLORS["offline"]))
        font = QFont(painter.font())
        size, weight = theme.font("badge")
        font.setPixelSize(size)
        font.setWeight(QFont.Weight(weight))
        painter.setFont(font)
        metrics = painter.fontMetrics()
        text = item.status
        width = min(
            max(28, option.rect.width() - theme.SPACE["xs"]),
            metrics.horizontalAdvance(text) + theme.SPACE["md"],
        )
        rect = QRect(0, 0, width, 20)
        rect.moveCenter(option.rect.center())
        painter.setPen(QPen(colour, 1))
        painter.setBrush(Qt.NoBrush)
        painter.drawRoundedRect(rect, theme.RADIUS["pill"], theme.RADIUS["pill"])
        painter.setPen(colour)
        if metrics.horizontalAdvance(text) > rect.width() - theme.SPACE["sm"]:
            text = metrics.elidedText(text, Qt.ElideRight, max(8, rect.width() - theme.SPACE["sm"]))
        painter.drawText(rect, Qt.AlignCenter, text)

    def _paint_action(self, painter: QPainter, option: QStyleOptionViewItem, item: TransferItem) -> None:
        self._fill_background(painter, option)
        palette = colors()
        font = QFont(painter.font())
        size, weight = theme.font("body-strong")
        font.setPixelSize(size)
        font.setWeight(QFont.Weight(weight))
        painter.setFont(font)
        enabled = item.action_enabled
        colour = QColor(palette["text"] if enabled else palette["text_faint"])
        if not enabled:
            colour.setAlpha(120)
        if option.state & QStyle.State_Selected:
            colour = option.palette.color(QPalette.HighlightedText)
        painter.setPen(colour)
        label_rect = QRect(option.rect)
        if item.has_overflow():
            label_rect.setWidth(label_rect.width() - OVERFLOW_WIDTH)
        painter.drawText(label_rect, Qt.AlignCenter, item.action_text() if item.action_enabled else "—")
        if item.has_overflow():
            glyph = icons.icon("more", colour.name(), 16)
            zone = QRect(option.rect.right() - OVERFLOW_WIDTH, option.rect.top(), OVERFLOW_WIDTH, option.rect.height())
            glyph.paint(painter, zone, Qt.AlignCenter)

    def _paint_name(self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex, item: TransferItem) -> None:
        """文件列：类型图标（20px，宪章 §2.3）+ 文件名**中间省略**。

        中间省略而不是尾部省略：文件名的尾部（扩展名）比中间那段更有信息量，
        "报告-2024-…-最终版.pdf" 比 "报告-2024-第三季…" 好认。
        """
        self._fill_background(painter, option)
        palette = colors()
        icon = index.data(Qt.DecorationRole)
        text_rect = QRect(option.rect)
        text_rect.setLeft(text_rect.left() + theme.SPACE["md"])
        if icon is not None and not icon.isNull():
            size = 20
            icon_rect = QRect(
                text_rect.left(),
                option.rect.center().y() - size // 2,
                size,
                size,
            )
            icon.paint(painter, icon_rect, Qt.AlignCenter)
            text_rect.setLeft(icon_rect.right() + theme.SPACE["sm"])
        text_rect.setRight(option.rect.right() - theme.SPACE["md"])
        colour = QColor(option.palette.color(QPalette.HighlightedText) if option.state & QStyle.State_Selected else palette["text"])
        painter.setPen(colour)
        font = QFont(painter.font())
        size, weight = theme.font("body")
        font.setPixelSize(size)
        font.setWeight(QFont.Weight(weight))
        painter.setFont(font)
        metrics = painter.fontMetrics()
        text = item.name or "（没有名字）"
        if metrics.horizontalAdvance(text) > text_rect.width():
            text = metrics.elidedText(text, Qt.ElideMiddle, text_rect.width())
        painter.drawText(text_rect, int(Qt.AlignLeft | Qt.AlignVCenter), text)


# ---------------------------------------------------------------------------
# 视图
# ---------------------------------------------------------------------------


class TransferTable(QTableView):
    """一张传输表（发送表 / 接收表各一张）。

    ``TransferRow`` 的行句柄仍然从 ``table`` 上开出来；``history_rows`` /
    ``history_paths`` / ``item(row, col)`` 这些老接口保留着，因为它们描述的是
    "这张表里现在有什么"，与实现无关。
    """

    cancel_requested = Signal(str)
    retry_requested = Signal(str)
    preview_requested = Signal(str, str)

    def __init__(self, direction: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.direction = direction
        self.setObjectName("TransferTable")
        self._model = TransferModel(direction, self)
        self.setModel(self._model)
        self.setShowGrid(False)
        self.setWordWrap(False)
        self.setSelectionBehavior(QTableView.SelectRows)
        self.setSelectionMode(QTableView.ExtendedSelection)
        self.setEditTriggers(QTableView.NoEditTriggers)
        self.setAlternatingRowColors(False)
        self.setFrameShape(QFrame.NoFrame)
        self.setMouseTracking(True)
        self.setItemDelegate(TransferDelegate(self))
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(self._on_context_menu)
        header = self.horizontalHeader()
        header.setHighlightSections(False)
        header.setStretchLastSection(False)
        header.setSectionsClickable(True)
        header.setSortIndicatorShown(True)
        self.setSortingEnabled(True)
        header.setSortIndicator(-1, Qt.AscendingOrder)
        # 「文件」是这张表里最该宽的列：它吃掉剩余宽度，其余按内容定宽。
        # 以前所有列都写死宽度，文件名只剩几十个像素，用户看到的就是一列「…」。
        header.setMinimumSectionSize(64)
        # 自绘的那几列必须给足宽度：药丸和「重试」是画出来的，ResizeToContents
        # 问的是 delegate 的 sizeHint，画出来的东西它算不准 —— 不给固定宽度，
        # 状态列会被压到只露出半个字。
        # 状态列要放得下手机那条路的文案（「已交给手机，等它下载」/「手机正在取 42%」）——
        # 窄了就会被裁成「已交给手…」，用户看不懂在等谁。
        fixed = {
            "状态": 128 if direction == "send" else 112,
            "操作": 76 if direction == "send" else 72,
            "时间": 64 if direction == "send" else 56,
            "来自": 104 if direction == "send" else 92,
            "发给": 104,
            "大小": 132 if direction == "send" else 84,
        }
        if direction == "send":
            fixed["进度"] = 120
            fixed["速度"] = 64
            fixed["剩余"] = 60
        else:
            # 接收方向（G-B）：进度/速度/剩余三列与发送方向同语义。速度列要给
            # 「185.78 MiB/s」留够宽度（60px 时实测被裁成「185.78 ⋯」）。
            fixed["进度"] = 104
            fixed["速度"] = 92
            fixed["剩余"] = 60
            fixed["时间"] = 52
        for column in range(len(self._model.columns)):
            name = self._model.column_name(column)
            if name in fixed:
                header.setSectionResizeMode(column, header.ResizeMode.Fixed)
                self.setColumnWidth(column, fixed[name])
            else:
                header.setSectionResizeMode(column, header.ResizeMode.ResizeToContents)
        # 「文件」列吃掉剩下的宽度（见 ``_fit_file_column``）：它是这张表里最该
        # 宽的列，而且窄了就只能显示一列「…」。
        header.setSectionResizeMode(0, header.ResizeMode.Interactive)
        self.setColumnWidth(0, 240)
        self.verticalHeader().setVisible(False)
        self.verticalHeader().setDefaultSectionSize(theme.ROW_HEIGHT)
        self.verticalHeader().setSectionResizeMode(self.verticalHeader().ResizeMode.Fixed)
        #: 历史行（已经落盘的文件）—— 视图行号，筛选/搜索之后会重算。
        self.history_rows: set[int] = set()
        #: 历史行 → (路径, 文件名)，右键、"另存为"、"重新发送" 要用。
        self.history_paths: dict[int, tuple[str, str]] = {}
        #: 「重新发送」的回调（``(path) -> None``）；没设时那一项是禁用的。
        self.resend_handler = None
        # 只有"行序整体变了"才重建历史行索引（筛选/搜索/排序会发 modelReset）。
        # 插入和删除**不能**挂重建：``add_history_row`` 每加一行就遍历全表是
        # O(n²)，1000 行时后半段会慢好几倍（性能自检里就是这么发现的）。
        # 新增行自己增量记账（见 :meth:`note_history_row`），隐藏/移除才重建。
        self._model.modelReset.connect(self._sync_history_rows)

    # -- 兼容壳 -------------------------------------------------------------

    def model(self) -> TransferModel:  # noqa: N802 - Qt naming
        return self._model

    def rowCount(self) -> int:  # noqa: N802 - Qt naming
        return self._model.rowCount()

    def item(self, row: int, column: int):  # noqa: N802 - Qt naming
        """``QTableWidget`` 时代的取格方式（测试和窗口都在用）。"""
        rows = self._model.rows
        if 0 <= row < len(rows):
            return _CellText(self, rows[row], column)
        return None

    def isRowHidden(self, row: int) -> bool:  # noqa: N802 - Qt naming
        rows = self._model.rows
        return not (0 <= row < len(rows))

    def setRowHidden(self, row: int, hidden: bool) -> None:  # noqa: N802 - Qt naming
        rows = self._model.rows
        if 0 <= row < len(rows):
            self._model.set_hidden(rows[row], hidden)

    def cellWidget(self, row: int, column: int):  # noqa: N802 - Qt naming
        """这一版没有"每格一个控件"；留个空实现，调用方不必特判。"""
        return None

    def visible_rows(self) -> int:
        return len(self._model.rows)

    def hide_all(self) -> int:
        """「清空记录」：把已经结束的行收起来，正在传的不动。

        收起是**隐藏**而不是删除：删一行会让下面所有行的下标上移，而活动传输
        行是按下标持有句柄的（见 ``docs/UI_LAYOUT.md`` §4.5）。
        """
        hidden = 0
        for item in list(self._model.items):
            if item.hidden or item.is_active():
                continue
            self._model.set_hidden(item, True)
            hidden += 1
        self._sync_history_rows()
        return hidden

    def busy_count(self) -> int:
        """**当前筛选下**看得见的进行中行数（筛选到"失败"时说"1 个进行中"是错的）。"""
        return len([item for item in self._model.rows if item.is_active()])

    def phone_busy_count(self) -> int:
        """当前看得见的、正在等手机取的条数（手机是 HTTP 来取，不是本机推）。"""
        return len(
            [
                item
                for item in self._model.rows
                if item.is_phone() and item.phone_phase in (PHONE_WAITING, PHONE_OFFLINE, PHONE_DOWNLOADING)
            ]
        )

    def set_filter(self, key: str) -> None:
        self._model.set_filter(key)
        self._sync_history_rows()

    def set_search(self, text: str) -> None:
        self._model.set_search(text)
        self._sync_history_rows()

    def filter_key(self) -> str:
        return self._model.filter_key

    def search_text(self) -> str:
        return self._model.search_text

    # -- 行数据 -------------------------------------------------------------

    def add_item(self, item: TransferItem, key: str = "") -> TransferItem:
        item.direction = self.direction
        if not item.when:
            item.when = time.strftime("%H:%M", time.localtime())
        item.when_ts = item.when_ts or time.time()
        effective = self._model.add(item, key=key)
        self.note_history_row(effective)
        return effective

    def add_history_row(
        self, name: str, path: str, peer: str, size_text: str, when: str, key: str = ""
    ) -> int:
        """一个已经收完的文件：已完成 + 「预览」+ ``⋯``（完整文件动作矩阵）。

        ``key`` 是 ``(方向, 对端, 内容)`` 的摘要（宪章 C8）：同一个文件收两次
        （哪怕传输号不同）只占一行；同名不同内容仍然是两行。
        """
        size = 0
        try:
            size = os.path.getsize(path)
        except OSError:
            size = 0
        item = TransferItem(
            key=str(key or f"history:{path}:{len(self._model.items)}"),
            direction="recv",
            name=name,
            peer=peer or "—",
            size_bytes=size,
            size_text=size_text or human_bytes(size),
            when=when or time.strftime("%H:%M", time.localtime()),
            when_ts=time.time(),
            status="已完成",
            kind=KIND_DONE,
            path=path,
            progress=1.0,
        )
        effective = self._model.add(item, key=str(key or item.key))
        self.note_history_row(effective)
        return self._model.row_of(effective)

    def note_history_row(self, item: TransferItem) -> None:
        """把**刚加进来的那一行**记进历史索引（增量，不遍历全表）。"""
        if not item.path:
            return
        row = self._model.row_of(item)
        if row < 0:
            return
        self.history_rows.add(row)
        self.history_paths[row] = (item.path, item.name)

    def _sync_history_rows(self) -> None:
        """全量重建历史行索引（筛选、搜索、排序、隐藏之后调用）。"""
        self.history_rows = set()
        self.history_paths = {}
        for row, item in enumerate(self._model.rows):
            if item.path:
                self.history_rows.add(row)
                self.history_paths[row] = (item.path, item.name)

    def row_of_item(self, item: TransferItem) -> int:
        return self._model.row_of(item)

    def _fit_file_column(self) -> None:
        """文件列 = 视口宽度减去其余列（至少 200px）。

        固定宽度 + Stretch 在这种"自绘列"的表上会让文件名列被挤成几个像素，
        所以这里自己算：别的列该多宽就多宽，剩下的全给文件名。
        """
        header = self.horizontalHeader()
        others = sum(self.columnWidth(c) for c in range(1, len(self._model.columns)))
        available = self.viewport().width() - others - 2
        # 别硬撑到 200：撑过头就是横向滚动条，操作列反而被推出屏幕。
        self.setColumnWidth(0, max(160, available))

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt naming
        super().resizeEvent(event)
        self._fit_file_column()

    def item_at(self, row: int) -> TransferItem | None:
        rows = self._model.rows
        return rows[row] if 0 <= row < len(rows) else None

    # -- 行内动作 -----------------------------------------------------------

    def action_text(self, row: int) -> str:
        item = self.item_at(row)
        return item.action_text() if item is not None else ""

    def action_menu(self, row: int) -> QMenu | None:
        item = self.item_at(row)
        return self._menu_for(item) if item is not None else None

    def invoke_action(self, item: TransferItem | None) -> None:
        """按一次操作格里的主按钮（取消 / 预览 / 重试 / 移除）。"""
        if item is None:
            return
        if not item.action_enabled:
            return
        label = item.action_text()
        if item.is_active() and label == "取消":
            self.cancel_requested.emit(item.transfer_id or item.key)
        elif label == "预览" and item.path:
            self.preview_requested.emit(item.path, item.name)
        elif label == "重试":
            self.retry_requested.emit(item.transfer_id or item.key)
        elif label == "移除" or (item.path and label != "预览"):
            self._model.set_hidden(item, True)
            self._sync_history_rows()
        elif label == "打开所在位置":
            from .widgets import _reveal_path  # noqa: PLC2701 - 同一个包内的动作实现

            _reveal_path(item.path)

    def _menu_for(self, item: TransferItem | None) -> QMenu | None:
        if item is None:
            return None
        if item.path:
            handler = self.resend_handler if callable(self.resend_handler) else None
            menu = file_actions_menu(self, item.path, item.name, resend=handler)
            return menu
        menu = QMenu(self)
        if item.is_active():
            action = menu.addAction("取消这次传输")
            action.triggered.connect(lambda _c=False, key=item.transfer_id or item.key: self.cancel_requested.emit(key))
        elif item.retryable():
            action = menu.addAction("重试")
            action.triggered.connect(lambda _c=False, key=item.transfer_id or item.key: self.retry_requested.emit(key))
        remove = menu.addAction("从列表里移除")
        remove.triggered.connect(lambda _c=False, it=item: self._remove_item(it))
        if item.reason:
            menu.addSeparator()
            reason = menu.addAction("原因：" + item.reason)
            reason.setEnabled(False)
        return menu

    def _remove_item(self, item: TransferItem) -> None:
        self._model.set_hidden(item, True)
        self._sync_history_rows()

    # -- 鼠标 / 键盘 --------------------------------------------------------

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 - Qt naming
        index = self.indexAt(event.position().toPoint())
        if not index.isValid():
            super().mouseReleaseEvent(event)
            return
        item = index.data(ROLE_ITEM)
        if not isinstance(item, TransferItem):
            super().mouseReleaseEvent(event)
            return
        if self._model.column_name(index.column()) == "操作":
            rect = self.visualRect(index)
            point = event.position().toPoint()
            if item.has_overflow() and point.x() >= rect.right() - OVERFLOW_WIDTH:
                menu = self._menu_for(item)
                if menu is not None:
                    menu.exec(event.globalPosition().toPoint())
                return
            self.invoke_action(item)
            return
        if index.column() == 0 and item.path:
            self.preview_requested.emit(item.path, item.name)
            return
        super().mouseReleaseEvent(event)

    def _on_context_menu(self, point) -> None:
        index = self.indexAt(point)
        if not index.isValid():
            return
        item = index.data(ROLE_ITEM)
        menu = self._menu_for(item if isinstance(item, TransferItem) else None)
        if menu is not None:
            menu.exec(self.viewport().mapToGlobal(point))

    def keyPressEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if event.key() in (Qt.Key_Return, Qt.Key_Enter):
            item = self.item_at(self.currentIndex().row())
            if item is not None:
                self.invoke_action(item)
                return
        super().keyPressEvent(event)


class _CellText:
    """``QTableWidgetItem`` 形状的取格句柄（读/写都落到模型里的那一行）。"""

    def __init__(self, table: TransferTable, item: TransferItem, column: int) -> None:
        self._table = table
        self._item = item
        self._column = column

    def text(self) -> str:
        return str(self._table.model().column_text(self._item, self._column))

    def setText(self, text: str) -> None:  # noqa: N802 - Qt naming
        name = self._table.model().column_name(self._column)
        if name == "文件":
            self._item.name = str(text)
        elif name in ("发给", "来自"):
            self._item.peer = str(text)
        elif name == "大小":
            self._item.size_text = str(text)
        elif name == "时间":
            self._item.when = str(text)
        elif name == "状态":
            self._item.status = str(text)
            self._item.kind = _kind_for_status(str(text))
        self._table.model().touch(self._item)

    def setToolTip(self, text: str) -> None:  # noqa: N802 - Qt naming
        return

    def setStyleSheet(self, _sheet: str) -> None:  # noqa: N802 - Qt naming
        return

    def setVisible(self, _visible: bool) -> None:  # noqa: N802 - Qt naming
        return


class _Field:
    """``QLabel`` 形状的行字段句柄（``row.status`` / ``row.title`` …）。"""

    def __init__(self, row: "TransferRow", name: str) -> None:
        self._row = row
        self._name = name

    def text(self) -> str:
        return str(getattr(self._row.item, self._name) or "")

    def setText(self, text: str) -> None:  # noqa: N802 - Qt naming
        setattr(self._row.item, self._name, str(text))
        self._row.touch()

    def setToolTip(self, text: str) -> None:  # noqa: N802 - Qt naming
        self._row.item.reason = self._row.item.reason or str(text)

    def setStyleSheet(self, sheet: str) -> None:  # noqa: N802 - Qt naming
        marker = "color:"
        if marker not in sheet:
            return
        value = sheet.split(marker, 1)[1].split(";", 1)[0].strip().lower()
        if value == theme.DANGER.lower():
            self._row.item.kind = KIND_FAILED
        elif value == theme.SUCCESS.lower():
            self._row.item.kind = KIND_DONE
        elif value == theme.WARNING.lower():
            self._row.item.kind = KIND_CANCELLED
        self._row.touch()

    def setVisible(self, _visible: bool) -> None:  # noqa: N802 - Qt naming
        return

    def fullText(self) -> str:  # noqa: N802 - Qt naming
        return self.text()


class _ProgressShim:
    """``QProgressBar`` 形状的进度句柄（0..1000 的千分比，和以前一样）。"""

    def __init__(self, row: "TransferRow") -> None:
        self._row = row

    def value(self) -> int:
        return int(round(max(0.0, min(1.0, self._row.item.progress)) * 1000))

    def setValue(self, permille: int) -> None:
        item = self._row.item
        item.progress = max(0.0, min(1.0, float(permille) / 1000.0))
        item.indeterminate = False
        # 手机通道：千分比换算回字节，好让「大小」列写「已发出 / 总量」。
        if item.channel == CHANNEL_PHONE and item.total_bytes > 0:
            item.sent_bytes = int(round(item.progress * item.total_bytes))
            item.size_text = item.phone_total_text()
            if item.is_active() and item.phone_phase not in (PHONE_TAKEN,):
                item.phone_phase = PHONE_DOWNLOADING
        self._row.touch()

    def setRange(self, low: int, high: int) -> None:
        self._row.item.indeterminate = high <= low
        self._row.touch()

    def setFormat(self, _text: str) -> None:
        return

    def setTextVisible(self, _visible: bool) -> None:
        return

    def setVisible(self, visible: bool) -> None:  # noqa: N802 - Qt naming
        self._row.item.show_progress = bool(visible)
        self._row.touch()

    def isVisible(self) -> bool:  # noqa: N802 - Qt naming
        return bool(self._row.item.show_progress)

    def setFixedHeight(self, _height: int) -> None:  # noqa: N802 - Qt naming
        return

    def setMinimumWidth(self, _width: int) -> None:  # noqa: N802 - Qt naming
        return


class _ActionShim(QObject):
    """``QPushButton`` 形状的操作句柄（``row.cancel_button``）。

    它是个 ``QObject``，而且 ``clicked`` 是真的 Qt 信号：占位行被真正的传输号
    接手时，窗口会 ``cancel_button.clicked.disconnect()`` 再接到新号上（行换了
    名字，按钮必须跟着换目标），普通回调经不起这么接。
    """

    clicked = Signal()

    def __init__(self, row: "TransferRow") -> None:
        super().__init__(row)
        self._row = row

    def text(self) -> str:
        return self._row.item.action_text()

    def setText(self, text: str) -> None:  # noqa: N802 - Qt naming
        self._row.item.action_override = "" if text == self._row.item.action_text() else str(text)
        self._row.touch()

    def isEnabled(self) -> bool:  # noqa: N802 - Qt naming
        return bool(self._row.item.action_enabled)

    def setEnabled(self, enabled: bool) -> None:  # noqa: N802 - Qt naming
        self._row.item.action_enabled = bool(enabled)
        self._row.touch()

    def click(self) -> None:
        self.clicked.emit()


class TransferRow(QObject):
    """一次传输在表里的那一行。

    行句柄不是控件：它拿着模型里的 ``TransferItem``，所以行数再多也只有一份数据。
    """

    cancel_requested = Signal(str)

    def __init__(
        self,
        transfer_id: str,
        title: str,
        direction: str,
        table: TransferTable,
        parent: QObject | None = None,
        key: str = "",
    ) -> None:
        super().__init__(parent)
        self.table = table
        self.transfer_id = transfer_id
        self.direction = direction
        self._finished = False
        self.peer_name = _peer_from_title(title)
        self.deleted = False
        # 「交给 V2408A：电影.mkv」这种标题里已经带着文件名了：文件列只写文件名，
        # 对端进「发给」列（宪章 §3.2 的列就是「文件 · 发给」）。
        file_name = _file_from_title(title)
        handoff = title.startswith("交给 ")
        candidate = TransferItem(
            key=str(key or transfer_id),
            direction=direction,
            name=file_name or title,
            peer=self.peer_name or "—",
            transfer_id=transfer_id,
            status="准备中…",
            kind=KIND_ACTIVE,
            # 「交给 手机：文件」这条路是 HTTP 发布（手机来取），不是 TCP 推；
            # 窗口那边一直是这么起标题的，这里认出来就不用他改代码。
            channel=CHANNEL_PHONE if handoff else CHANNEL_PEER,
        )
        # 同一个 (方向, 对端, 内容) 已经有一行时，模型会把它还给我们：这一行的
        # 进度、状态全都更新到那一行上（宪章 C8：一个文件在一张表里只占一行）。
        self.item = table.add_item(candidate, key=str(key or transfer_id))
        self.row = table.row_of_item(self.item)

        self.title = _Field(self, "name")
        self.peer = _Field(self, "peer")
        self.size = _Field(self, "size_text")
        #: 旧名字：卡片时代"大小 / 速度 / 剩余"那一行叫 detail。
        self.detail = self.size
        self.speed = _Field(self, "speed_display")
        self.when = _Field(self, "when")
        self.status = _Field(self, "status")
        self.bar = _ProgressShim(self)
        self.cancel_button = _ActionShim(self)
        # 行内那个主按钮点下去 = 取消这次传输（传输号变了会重新接，见窗口的
        # ``_adopt_pending_row``）。表上自绘的同一格走 ``table.invoke_action``。
        self.cancel_button.clicked.connect(self._emit_cancel)

    def _emit_cancel(self) -> None:
        self.cancel_requested.emit(self.transfer_id)

    def set_transfer_id(self, transfer_id: str) -> None:
        """占位号被真正的传输号接手（窗口的 ``_adopt_pending_row`` 调）。"""
        self.transfer_id = transfer_id
        self.item.transfer_id = transfer_id
        self.item.key = transfer_id

    # -- 进度 ---------------------------------------------------------------

    def touch(self) -> None:
        self.table.model().touch(self.item)

    # -- 手机通道（HTTP 发布 + 手机来取，见 docs/REAL_DEVICE_FINDINGS.md §2）------
    #
    # 手机是纯 HTTP 客户端：本机推不过去，"传到哪了"只有服务端知道，所以这一组
    # 接口把**服务端已经发出去的字节**当进度，并且把阶段说清楚。四个阶段之外
    # 不许写「完成」——手机真正取走之前写 sent 就是撒谎（真机联调 P0）。

    def set_channel(self, channel: str) -> None:
        self.item.channel = CHANNEL_PHONE if channel == CHANNEL_PHONE else CHANNEL_PEER
        self.touch()

    def set_phone_handoff(self, total_bytes: int = 0, waiting_reason: str = "") -> None:
        """刚发布完，等手机来取：进度条待定态，状态写「已交给手机，等它下载」。"""
        item = self.item
        item.channel = CHANNEL_PHONE
        item.phone_phase = PHONE_OFFLINE if waiting_reason else PHONE_WAITING
        item.total_bytes = int(total_bytes or item.total_bytes or 0)
        item.sent_bytes = 0
        item.progress = 0.0
        item.indeterminate = True
        item.kind = KIND_ACTIVE
        if waiting_reason:
            # 药丸只有 128px：状态写一句话，原因进悬停提示（宪章 §3.2 失败/等待都这么处理）。
            item.status = "手机不在线，等它回来"
            item.reason = str(waiting_reason)
        else:
            item.status = "已交给手机，等它下载"
        if item.total_bytes:
            item.size_text = human_bytes(item.total_bytes)
        self.touch()

    def set_phone_progress(self, sent_bytes: int, total_bytes: int = 0) -> None:
        """手机正在取：真实百分比 = 服务端已发出的字节 / 总量。"""
        item = self.item
        item.channel = CHANNEL_PHONE
        total = int(total_bytes or item.total_bytes or 0)
        if total:
            item.total_bytes = total
        sent = max(0, int(sent_bytes or 0))
        item.sent_bytes = min(sent, item.total_bytes) if item.total_bytes else sent
        if sent <= 0 and item.phone_phase in (PHONE_WAITING, PHONE_OFFLINE, ""):
            # 还没开始取：保持"等它下载"的待定态。一个 0% 的空进度条会让人以为卡住了
            # （phone-transfer 的期望，也是真机上最容易被误读的状态）。
            item.indeterminate = True
            item.phone_phase = item.phone_phase or PHONE_WAITING
            item.kind = KIND_ACTIVE
            if not item.status.startswith("手机不在线"):
                item.status = "已交给手机，等它下载"
            item.sent_bytes = 0
            self.touch()
            return
        if item.total_bytes > 0:
            item.progress = max(0.0, min(1.0, item.sent_bytes / item.total_bytes))
            item.size_text = item.phone_total_text()
            item.indeterminate = False
            item.phone_phase = PHONE_DOWNLOADING
            item.status = f"手机正在取 {item.percent():.0f}%"
        else:
            # 总量都还不知道：待定态，但状态别说是"正在取 xx%"。
            item.indeterminate = True
            item.phone_phase = PHONE_DOWNLOADING
            item.status = "手机正在取…"
        item.kind = KIND_ACTIVE
        self.touch()

    def set_phone_taken(self, message: str = "手机已取走") -> None:
        """手机真的取走了 —— 这才是这一行的成功终态。"""
        item = self.item
        item.channel = CHANNEL_PHONE
        item.phone_phase = PHONE_TAKEN
        if item.total_bytes:
            item.sent_bytes = item.total_bytes
        item.progress = 1.0
        item.indeterminate = False
        item.kind = KIND_DONE
        item.status = message or "手机已取走"
        item.reason = ""
        self.touch()

    def set_phone_offline(self, reason: str = "手机现在不在线") -> None:
        """手机熄屏 / 离开页面：**不是失败**，文件留着等它回来。"""
        item = self.item
        item.channel = CHANNEL_PHONE
        item.phone_phase = PHONE_OFFLINE
        item.indeterminate = item.total_bytes <= 0 or item.sent_bytes <= 0
        item.kind = KIND_ACTIVE
        item.status = "手机不在线，等它回来"
        item.reason = str(reason or "手机现在不在线")
        self.touch()

    def set_phone_failed(self, reason: str) -> None:
        """真失败：红药丸 + 中文原因（悬停）+ 行内「重试」。"""
        item = self.item
        item.channel = CHANNEL_PHONE
        item.phone_phase = PHONE_FAILED
        item.indeterminate = False
        item.kind = KIND_FAILED
        item.status = "失败"
        item.reason = str(reason or "原因不明")
        item.action_override = ""
        item.action_enabled = True
        self.touch()

    def set_paths(self, paths: list) -> None:
        """这次要传的源文件（「重试」要按原样再发一次）。"""
        self.item.paths = [str(path) for path in paths]
        self.touch()

    def set_files(self, text: str) -> None:
        if text and text != self.item.name:
            self.item.name = str(text)
            self.touch()

    def set_total(self, total: int) -> None:
        if total:
            self.item.size_bytes = int(total)
            self.item.size_text = human_bytes(total)
            self.touch()

    def update_progress(self, done: int, total: int, speed: float, eta: float | None) -> None:
        total = max(1, int(total))
        done = max(0, min(int(done), total))
        item = self.item
        item.progress = done / total
        item.indeterminate = False
        item.speed_bps = float(speed or 0)
        item.eta = eta
        if self.direction == "send":
            item.size_text = f"{human_bytes(done)} / {human_bytes(total)}"
            item.size_bytes = total
        else:
            # 「大小」列是文件大小，不是"已经收了多少"：接收过程中它必须保持
            # 不变（以前这里写 done，用户会看到 0 B→60 MB→64 MB 一路变，像是
            # 文件越收越大）。已收多少看「进度」列的百分比/进度条。
            item.size_text = human_bytes(total)
            item.size_bytes = total
        self.touch()

    def set_status(self, text: str, color: str = "") -> None:
        item = self.item
        item.status = str(text)
        item.kind = _kind_for_status(str(text))
        # 手机行："手机正在取 42%" 这类文案由调用方给，我顺手把阶段/进度补齐，
        # 免得状态说"正在取"而进度条还停在待定态（两处说两件事）。
        if item.channel == CHANNEL_PHONE:
            if "已取走" in item.status:
                item.phone_phase = PHONE_TAKEN
                item.kind = KIND_DONE
            elif "不在线" in item.status:
                item.phone_phase = PHONE_OFFLINE
            elif "正在取" in item.status:
                item.phone_phase = PHONE_DOWNLOADING
                item.indeterminate = item.total_bytes <= 0
            elif "已交给" in item.status or "等它" in item.status:
                item.phone_phase = PHONE_WAITING
                item.indeterminate = True
        if color:
            lowered = color.lower()
            if lowered == theme.DANGER.lower():
                item.kind = KIND_FAILED
            elif lowered == theme.SUCCESS.lower():
                item.kind = KIND_DONE
            elif lowered == theme.WARNING.lower():
                item.kind = KIND_CANCELLED
        self.touch()

    def finish(self, ok: bool, message: str = "") -> None:
        """把这一行放进终态（幂等：两条结束路径都可能调到这里）。"""
        if self._finished:
            return
        self._finished = True
        item = self.item
        if self.direction == "send" and ok:
            item.progress = 1.0
        if ok:
            # 成功时 message 通常就是「完成」/「手机已取走」这种人话，直接用。
            item.status = message or "完成"
            item.kind = KIND_DONE
        elif "取消" in (message or ""):
            item.status = "已取消"
            item.kind = KIND_CANCELLED
            item.reason = message or "你取消了这次传输"
        else:
            # 失败：药丸只写两个字，**原因进悬停提示**（宪章 §3.2：失败行悬停显示原因）。
            # 以前把整句原因塞进药丸，96px 的列里被裁成「拒绝（对方可能开」。
            item.status = "失败"
            item.kind = KIND_FAILED
            item.reason = message or "未知原因"
        item.action_override = ""
        item.action_enabled = True
        self.touch()

    def hide(self) -> None:
        """把这一行收起来（不删除 —— 删除会让下标上移，见 §4.5）。"""
        self.table.model().set_hidden(self.item, True)
        self.table._sync_history_rows()  # noqa: SLF001 - 同一张表的内部记账

    def deleteLater(self) -> None:  # noqa: N802 - Qt naming
        self.deleted = True
        self.hide()
        super().deleteLater()


def _file_from_title(title: str) -> str:
    """``交给 手机：电影.mkv`` → ``电影.mkv``（其余标题返回空串）。"""
    if not title.startswith("交给 "):
        return ""
    rest = title[len("交给 "):]
    for separator in ("：", ":"):
        if separator in rest:
            return rest.split(separator, 1)[1].strip()
    return ""


def _peer_from_title(title: str) -> str:
    """``发送到 客厅台式机`` / ``交给 手机：a.bin`` → 设备名。"""
    for prefix in ("发送到 ", "接收自 ", "交给 "):
        if title.startswith(prefix):
            rest = title[len(prefix):]
            for separator in ("：", ":"):
                if separator in rest:
                    rest = rest.split(separator, 1)[0]
            return rest.strip()
    return ""


# ---------------------------------------------------------------------------
# 空状态
# ---------------------------------------------------------------------------


class _EmptyState(QFrame):
    """空表的样子：居中图标 + 一句话 + 一个主按钮（宪章 §3.2）。"""

    action_clicked = Signal()
    clear_filter_clicked = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("EmptyState")
        row = QVBoxLayout(self)
        row.setContentsMargins(theme.SPACE["2xl"], theme.SPACE["2xl"], theme.SPACE["2xl"], theme.SPACE["2xl"])
        row.setSpacing(theme.SPACE["md"])
        row.addStretch(1)
        self.icon = QLabel()
        self.icon.setAlignment(Qt.AlignCenter)
        row.addWidget(self.icon, 0, Qt.AlignHCenter)
        self.title = QLabel("")
        self.title.setObjectName("EmptyTitle")
        self.title.setAlignment(Qt.AlignCenter)
        row.addWidget(self.title)
        self.hint = QLabel("")
        self.hint.setObjectName("Subtitle")
        self.hint.setAlignment(Qt.AlignCenter)
        self.hint.setWordWrap(True)
        row.addWidget(self.hint)
        buttons = QHBoxLayout()
        buttons.setSpacing(theme.SPACE["sm"])
        buttons.addStretch(1)
        self.action = QPushButton("")
        self.action.setObjectName("Primary")
        self.action.clicked.connect(self.action_clicked)
        buttons.addWidget(self.action)
        self.clear_filter = QPushButton("清空筛选")
        self.clear_filter.setObjectName("Ghost")
        self.clear_filter.clicked.connect(self.clear_filter_clicked)
        buttons.addWidget(self.clear_filter)
        buttons.addStretch(1)
        row.addLayout(buttons)
        row.addStretch(1)
        self.set_icon("inbox")

    def set_icon(self, name: str) -> None:
        self.icon.setPixmap(icons.icon(name, colors()["text_faint"], 40).pixmap(40, 40))


class TransfersView(QWidget):
    """传输页：筛选条 + 「接收｜发送」两张表 + 空状态。"""

    open_folder_clicked = Signal()
    change_folder_clicked = Signal()
    clear_clicked = Signal()
    #: 行内动作（主按钮 / ``⋯`` 菜单）从这一层转给窗口。
    cancel_requested = Signal(str)
    retry_requested = Signal(str)
    preview_requested = Signal(str, str)
    #: 空状态里的主按钮：「去发送一个文件」。
    compose_requested = Signal()
    #: 传输页顶部的「← 返回会话」（宪章 §3.1：传输页顶部给一个返回入口）。
    back_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        root = QVBoxLayout(self)
        root.setContentsMargins(theme.SPACE["lg"], theme.SPACE["md"], theme.SPACE["lg"], theme.SPACE["md"])
        root.setSpacing(theme.SPACE["md"])

        toolbar = QFrame()
        toolbar.setObjectName("Toolbar")
        rows = QVBoxLayout(toolbar)
        rows.setContentsMargins(0, 0, 0, 0)
        rows.setSpacing(theme.SPACE["sm"])

        # 第一行：返回 + 方向 + 右侧 ⋯（宪章 §3.1：传输页顶部给「← 返回」）
        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.setSpacing(theme.SPACE["sm"])

        # 页面入口只有侧栏底部导航，主区顶部不再有分段控件；传输页自己给一个返回。
        self.back_button = QPushButton("  ←  返回会话")
        self.back_button.setObjectName("Ghost")
        self.back_button.setToolTip("回到和选中设备的会话")
        self.back_button.clicked.connect(self.back_requested)
        top.addWidget(self.back_button)

        self.segmented = SegmentedControl((("recv", "接收"), ("send", "发送")), "recv")
        self.segmented.changed.connect(self._on_switch)
        top.addWidget(self.segmented)
        top.addStretch(1)

        self.more_button = icon_button("more", "更多：清空记录")
        self.more_menu = QMenu(self.more_button)
        self.clear_action = self.more_menu.addAction("清空记录…")
        self.clear_action.setToolTip("只清空已完成的传输记录，正在传的不受影响")
        self.clear_action.triggered.connect(self.clear_clicked)
        self.more_button.setMenu(self.more_menu)
        top.addWidget(self.more_button)
        rows.addLayout(top)

        # 第二行：筛选药丸 + 搜索（吃掉剩余宽度）+ 主按钮
        bottom = QHBoxLayout()
        bottom.setContentsMargins(0, 0, 0, 0)
        bottom.setSpacing(theme.SPACE["sm"])
        self.filter_row = bottom
        self.pills: dict[str, QPushButton] = {}
        for key, label in FILTERS:
            chip = QPushButton(label)
            chip.setObjectName("FilterChip")
            chip.setCheckable(True)
            chip.setCursor(Qt.PointingHandCursor)
            chip.setChecked(key == "all")
            chip.clicked.connect(lambda _c=False, k=key: self.set_filter(k))
            self.pills[key] = chip
            bottom.addWidget(chip)

        self.search = QLineEdit()
        self.search.setObjectName("Search")
        self.search.setPlaceholderText("搜索文件或设备")
        self.search.setClearButtonEnabled(True)
        self.search.setMinimumWidth(180)
        self.search.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.search.textChanged.connect(self._on_search)
        bottom.addWidget(self.search, 1)

        self.summary = QLabel("")
        self.summary.setObjectName("Subtitle")
        bottom.addWidget(self.summary)

        #: 主按钮只有一个；「清空记录」收进 ``⋯``（宪章 §3.1）。
        #: 「更改接收位置」的正主是**设置页**（那里才有当前路径可看），
        #: 这里不再重复摆一份（铁律 1：一个意图一个入口）。
        self.folder_button = QPushButton("  打开接收文件夹")
        self.folder_button.setIcon(icons.icon("folder", theme.current_text_color(), 16))
        self.folder_button.clicked.connect(self.open_folder_clicked)
        bottom.addWidget(self.folder_button)
        rows.addLayout(bottom)
        root.addWidget(toolbar)

        self.stack = QStackedWidget()
        self.recv_table = TransferTable("recv")
        self.send_table = TransferTable("send")
        for table in (self.recv_table, self.send_table):
            table.cancel_requested.connect(self.cancel_requested)
            table.retry_requested.connect(self.retry_requested)
            table.preview_requested.connect(self.preview_requested)
        self.tables = QStackedWidget()
        self.tables.addWidget(self.recv_table)
        self.tables.addWidget(self.send_table)

        self.empty = _EmptyState()
        self.empty.action_clicked.connect(self.compose_requested)
        self.empty.clear_filter_clicked.connect(self.clear_filters)
        self.stack.addWidget(self.tables)
        self.stack.addWidget(self.empty)
        root.addWidget(self.stack, 1)

        self._refresh_empty()
        for table in (self.recv_table, self.send_table):
            table.model().modelReset.connect(self._refresh_empty)
            table.model().rowsInserted.connect(lambda *_: self._refresh_empty())
            table.model().rowsRemoved.connect(lambda *_: self._refresh_empty())

    # -- 切换 ---------------------------------------------------------------

    def _on_switch(self, key: str) -> None:
        self.tables.setCurrentWidget(self.recv_table if key == "recv" else self.send_table)
        self._refresh_empty()

    def current(self) -> str:
        return self.segmented.current() or "recv"

    def show_direction(self, direction: str, emit: bool = False) -> None:
        """把「发送」或「接收」那张表翻到前面（一次传输刚开始时）。"""
        key = "send" if direction == "send" else "recv"
        if self.current() == key and not emit:
            self.tables.setCurrentWidget(self.recv_table if key == "recv" else self.send_table)
            self._refresh_empty()
            return
        self.segmented.set_current(key, emit=emit)
        self.tables.setCurrentWidget(self.recv_table if key == "recv" else self.send_table)
        self._refresh_empty()

    def table(self, direction: str) -> TransferTable:
        return self.send_table if direction == "send" else self.recv_table

    def visible_table(self) -> TransferTable:
        return self.recv_table if self.current() == "recv" else self.send_table

    # -- 筛选 / 搜索 --------------------------------------------------------

    def set_filter(self, key: str) -> None:
        chosen = key if key in self.pills else "all"
        for pill_key, pill in self.pills.items():
            pill.setChecked(pill_key == chosen)
            pill.style().unpolish(pill)
            pill.style().polish(pill)
        for table in (self.recv_table, self.send_table):
            table.set_filter(chosen)
        self._refresh_empty()

    def filters(self) -> str:
        return self.recv_table.filter_key()

    def _on_search(self, text: str) -> None:
        for table in (self.recv_table, self.send_table):
            table.set_search(text)
        self._refresh_empty()

    def clear_filters(self) -> None:
        self.search.clear()
        self.set_filter("all")

    def filtered(self) -> bool:
        return bool(self.search.text().strip()) or self.filters() != "all"

    # -- 空状态 -------------------------------------------------------------

    def _refresh_empty(self) -> None:
        table = self.visible_table()
        visible = table.visible_rows()
        self.empty.setVisible(visible == 0)
        self.stack.setCurrentWidget(self.empty if visible == 0 else self.tables)
        if visible:
            busy = table.busy_count()
            phones = table.phone_busy_count()
            parts = [f"共 {visible} 条"]
            if busy:
                parts.append(f"{busy} 个进行中")
            if phones:
                # 手机那条路是"发布 + 等它来取"，和 TCP 推送不是一件事，单独说。
                parts.append(f"{phones} 个手机在取")
            self.summary.setText(" · ".join(parts))
        else:
            self.summary.setText("")
        if visible:
            return
        filtered = self.filtered()
        self.empty.clear_filter.setVisible(filtered)
        if filtered:
            self.empty.set_icon("search")
            self.empty.title.setText("没有符合条件的记录")
            self.empty.hint.setText("换个筛选或清空搜索词再看。")
            self.empty.action.setText("去发送一个文件")
            return
        if self.current() == "recv":
            self.empty.set_icon("inbox")
            self.empty.title.setText("还没有收到文件")
            self.empty.hint.setText("别人发过来时会出现在这里，传完的文件也能从这里打开。")
        else:
            self.empty.set_icon("inbox")
            self.empty.title.setText("还没有发送过文件")
            self.empty.hint.setText("在左边的会话里选文件发给对方，进度会显示在这里。")
        self.empty.action.setText("去发送一个文件")

    def refresh_empty(self) -> None:
        """再问一次可见表里有没有行（隐藏一行不会发 ``rowsInserted``）。"""
        self._refresh_empty()

    def set_summary(self, text: str) -> None:
        self.summary.setText(text)

    def set_receive_dir(self, path: str) -> None:
        self.folder_button.setToolTip(f"打开接收文件夹：{path}")
        self.folder_button.setText("  打开接收文件夹")


__all__ = [
    "FILTERS",
    "KIND_ACTIVE",
    "KIND_CANCELLED",
    "KIND_DONE",
    "KIND_FAILED",
    "RECV_COLUMNS",
    "SEND_COLUMNS",
    "TransferItem",
    "TransferModel",
    "TransferRow",
    "TransferTable",
    "TransfersView",
]
