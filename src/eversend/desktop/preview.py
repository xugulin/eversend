"""文件预览：能看的直接看，不能看的说清楚。

宪章 §6 的类型矩阵，一行行落在这里：

* **图片**：适应窗口（**不放大超过 100%**）↔ 原始大小；滚轮以鼠标为中心缩放，
  按住拖拽平移；大图解码在工作线程里做，主线程不卡。
* **视频**：真封面帧（有 FFmpeg 时是视频里的一帧）+ 「▶ 播放」+ 时长；
* **音频 / 语音**：「▶ 播放」+ 时长；
* **文本 / 代码**：前 256 KiB 等宽只读、可选中、可复制，超出在末尾说明；
* **其它二进制**：类型图标 + 名字 + 大小 + 三个动作，**不装作能看**。

窗口本身按规格：标题 = 文件名，工具栏 = `缩放− / 缩放+ / 适应窗口 / 原始大小 /
上一张 / 下一张 / 另存为 / 打开 / 在文件夹中显示`，键盘 `Esc / ← / → / Ctrl+S /
Ctrl+0 / Ctrl+1`。

判定顺序是"扩展名优先，再闻一闻内容"：``.png`` 这种一眼就知道的走扩展名；
没有扩展名或者扩展名不认识时，看前 4 KiB 能不能当文本解开（有 NUL 字节或者命中
zip/PNG/PDF/ELF 这些魔数就直接算二进制）。这样 README、日志、配置这些"没有
``.txt`` 后缀"的文件也能预览，三端用的是同一份判定。
"""

from __future__ import annotations

import os
import re
import subprocess
import threading

from PySide6.QtCore import QObject, QPoint, QPointF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import (
    QFont,
    QImage,
    QImageReader,
    QKeySequence,
    QPixmap,
    QShortcut,
    QTextCursor,
)
from PySide6.QtWidgets import (
    QButtonGroup,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QStyle,
    QStyleOption,
    QStylePainter,
    QVBoxLayout,
    QWidget,
)

from ..core import media, platform_open
from ..core.model import human_bytes
from . import icons, theme

#: 各类文件的扩展名。图片和视频这两类的判定要和聊天气泡里"长什么样"一致，
#: 不然同一个文件在气泡里是缩略图、点开却是"没有可预览的内容"。
IMAGE_EXTENSIONS = {
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".ico", ".tif", ".tiff", ".svg", ".avif",
}
VIDEO_EXTENSIONS = {
    ".mp4", ".mkv", ".mov", ".avi", ".webm", ".m4v", ".flv", ".wmv", ".mpg", ".mpeg", ".ts", ".3gp",
}
AUDIO_EXTENSIONS = {
    ".mp3", ".m4a", ".aac", ".wav", ".flac", ".ogg", ".opus", ".oga", ".wma", ".amr", ".aiff",
}
TEXT_EXTENSIONS = {
    ".txt", ".md", ".markdown", ".rst", ".log", ".json", ".yaml", ".yml", ".toml", ".ini",
    ".cfg", ".conf", ".csv", ".tsv", ".xml", ".html", ".htm", ".css", ".js", ".mjs", ".cjs",
    ".ts", ".tsx", ".jsx", ".py", ".rb", ".go", ".rs", ".java", ".kt", ".kts", ".c", ".h",
    ".cpp", ".hpp", ".cc", ".sh", ".bash", ".zsh", ".fish", ".ps1", ".bat", ".cmd", ".sql",
    ".dockerfile", ".gitignore", ".env", ".properties", ".gradle", ".lua", ".php", ".pl",
    ".swift", ".m", ".mm", ".cs", ".scala", ".clj", ".vim", ".tex", ".srt", ".vtt",
}

#: 预览文本时最多读这么多（再长就不是"看一眼"，是编辑器的活）。
TEXT_PREVIEW_BYTES = 256 * 1024

#: 判断"是不是文本"时闻一下的字节数。
SNIFF_BYTES = 4096

#: 超过这个大小、或者像素数超过 :data:`ASYNC_PIXELS` 的图片，解码放工作线程。
#: 小图同步解开（十几毫秒就够了），大图异步 —— 打开期间把主窗口卡住 >100ms
#: 就是 bug（宪章 §6）。
#:
#: 阈值是**实测**定的：真机拍的照片 3072×4096（12.6 MP，2.7 MB JPEG）在本机
#: 解码要 **268 ms** —— 只看"文件多大"会漏掉它（JPEG 压得很小、像素很多），
#: 所以像素数才是主判据（4 MP 大约 40~60 ms，是一帧的水平）。
ASYNC_DECODE_BYTES = 4 * 1024 * 1024
ASYNC_PIXELS = 4_000_000

#: 同一目录里"上一张 / 下一张"最多认多少个兄弟文件。
GALLERY_LIMIT = 500

#: 画布内边距（4 的倍数，宪章 §2）。
CANVAS_PAD = 8


def preview_kind(path: str) -> str:
    """``image`` / ``video`` / ``audio`` / ``text`` / ``binary`` / ``folder``。

    文件夹也能"预览"：不摊开内容，但说清楚里面有多少东西 —— 待发的是文件夹时，
    用户最想确认的就是"我选中的是不是那个目录"。
    """
    if os.path.isdir(str(path)):
        return "folder"
    extension = os.path.splitext(str(path))[1].lower()
    if extension in IMAGE_EXTENSIONS:
        return "image"
    if extension in VIDEO_EXTENSIONS:
        return "video"
    if extension in AUDIO_EXTENSIONS:
        return "audio"
    if extension in TEXT_EXTENSIONS:
        return "text"
    if extension == ".pdf":
        return "binary"  # 没有 PDF 渲染器，别装作能看
    return "text" if looks_like_text(path) else "binary"


def looks_like_text(path: str, limit: int = SNIFF_BYTES) -> bool:
    """A cheap "is this something a human can read" test.

    NUL bytes are the giveaway for binary; everything else gets decoded strictly
    as UTF-8 (files saved as GBK fall through to the binary branch, which is the
    honest answer for a preview that cannot know the encoding for sure).
    """
    try:
        with open(path, "rb") as handle:
            chunk = handle.read(limit)
    except OSError:
        return False
    if not chunk:
        return True
    if b"\x00" in chunk:
        return False
    if _is_probably_binary_magic(chunk):
        return False
    try:
        chunk.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def _is_probably_binary_magic(chunk: bytes) -> bool:
    """A few signatures that are binary even though they contain no NUL early on."""
    magic = (
        b"PK\x03\x04",  # zip / docx / apk
        b"\x1f\x8b",  # gzip
        b"\x89PNG",
        b"\xff\xd8\xff",  # jpeg
        b"GIF8",
        b"%PDF",
        b"\x7fELF",
        b"SQLite format 3",
        b"Rar!\x1a",
        b"7z\xbc\xaf",
    )
    return chunk.startswith(magic)


def read_text_preview(path: str, limit: int = TEXT_PREVIEW_BYTES) -> tuple[str, bool]:
    """(text, truncated) —— 先按 UTF-8 读，不成就退回 GB18030。

    截断点常常落在**一个多字节字符中间**（256 KiB 不是 3 的倍数，中文文件几乎
    必然如此）：这时严格 UTF-8 会失败，直接退到 GB18030 就会把整段中文解成
    "锟斤拷"。所以截断过的内容先把末尾那一两个不完整的字节去掉再解。
    """
    try:
        size = os.path.getsize(path)
    except OSError:
        return "", False
    truncated = size > limit
    with open(path, "rb") as handle:
        chunk = handle.read(limit)
    if truncated:
        chunk = _trim_partial_utf8(chunk)
    for encoding in ("utf-8", "gb18030"):
        try:
            return chunk.decode(encoding), truncated
        except UnicodeDecodeError:
            continue
    return chunk.decode("utf-8", "replace"), truncated


def _trim_partial_utf8(chunk: bytes) -> bytes:
    """Drop a trailing, incomplete UTF-8 sequence (at most 3 bytes)."""
    for back in range(1, 4):
        if len(chunk) <= back:
            break
        byte = chunk[-back]
        if byte < 0x80:
            return chunk  # 结尾是一个完整的 ASCII 字节
        if byte >= 0xC0:  # 多字节序列的首字节
            needed = 2 if byte < 0xE0 else 3 if byte < 0xF0 else 4
            return chunk[:-back] if needed > back else chunk
    return chunk


def tile_pixmap(path: str, size: int, data_dir: str = "") -> QPixmap | None:
    """A square thumbnail for a file, or ``None`` when we have no picture.

    Images are scaled-and-cropped to fill the tile; videos get a real poster
    frame when FFmpeg is around (``media.video_thumbnail``).  Everything else
    gets an icon instead — see :func:`tile_icon_name`.
    """
    kind = preview_kind(path)
    source = ""
    if kind == "image":
        source = path
    elif kind == "video":
        source = media.video_thumbnail(path, data_dir) or ""
    if not source:
        return None
    pixmap = QPixmap(source)
    if pixmap.isNull():
        return None
    scaled = pixmap.scaled(
        size, size, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation
    )
    # 居中裁一块方的：缩略图排成一行时才不会高矮不一。
    x = max(0, (scaled.width() - size) // 2)
    y = max(0, (scaled.height() - size) // 2)
    return scaled.copy(x, y, size, size)


def _count_tree(path: str) -> tuple[int, int, int]:
    """(文件数, 子目录数, 总字节) —— 只为把一句话说清楚，失败就都给 0。"""
    files = folders = total = 0
    try:
        for root, dirs, names in os.walk(path):
            folders += len(dirs)
            for name in names:
                files += 1
                try:
                    total += os.path.getsize(os.path.join(root, name))
                except OSError:
                    continue
    except OSError:
        pass
    return files, folders, total


def tile_icon_name(path: str) -> str:
    """Which line icon stands for this file when there is no picture."""
    if os.path.isdir(str(path)):
        return "folder"
    kind = preview_kind(path)
    if kind == "image":
        return "image"
    if kind == "video":
        return "film"
    if kind == "audio":
        return "music"
    if kind == "text":
        return "code"
    if kind == "folder":
        return "folder"
    extension = os.path.splitext(str(path))[1].lower()
    if extension in (".zip", ".tar", ".gz", ".bz2", ".xz", ".7z", ".rar"):
        return "archive"
    return "file"


def duration_text(path: str, data_dir: str = "") -> str:
    """``时长 1:23`` —— 视频/音频的那一项事实，拿不到就空着。"""
    seconds = media.media_duration(path, data_dir)
    try:
        seconds = float(seconds or 0)
    except (TypeError, ValueError):
        seconds = 0.0
    if seconds <= 0:
        return ""
    minutes, rest = divmod(int(round(seconds)), 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"时长 {hours}:{minutes:02d}:{rest:02d}"
    return f"时长 {minutes}:{rest:02d}"


def _natural_key(text: str) -> list:
    """``图 2`` 排在 ``图 10`` 前面（上一张/下一张要按人眼的顺序走）。"""
    return [int(part) if part.isdigit() else part.lower() for part in re.split(r"(\d+)", str(text))]


def sibling_files(path: str, limit: int = GALLERY_LIMIT) -> list[str]:
    """同一个目录里"能一起翻看"的兄弟文件（上一张 / 下一张用）。

    自动按目录取，是为了让"点开一张图 → 左右翻"在**任何入口**都好用（气泡、
    待发方块、传输表都只是给了一个路径）。只收同一类东西（图片配图片、文本配
    文本），免得翻着翻着跳出一个二进制文件。
    """
    if not path:
        return []
    if not os.path.isfile(path):
        return [path]
    folder = os.path.dirname(os.path.abspath(path)) or "."
    kind = preview_kind(path)
    try:
        names = sorted(os.listdir(folder), key=_natural_key)
    except OSError:
        return [os.path.abspath(path)]
    found = [
        os.path.join(folder, name)
        for name in names
        if os.path.isfile(os.path.join(folder, name))
        and preview_kind(os.path.join(folder, name)) == kind
    ][:limit]
    target = os.path.abspath(path)
    if target not in found:
        found.append(target)
    return found


class BackgroundTask(QObject):
    """把一段活丢到工作线程跑，结果用**信号**送回 GUI 线程。

    为什么不是 ``QTimer.singleShot(0, callback)``：那行代码是在**工作线程**里执行的，
    而 Qt 的定时器只在有事件循环的线程里跑 —— GUI 线程永远收不到那次回调。真机上
    的表现就是预览一张大图时永远停在「正在解码…」、文件夹永远停在「正在统计…」。
    信号跨线程是自动排队投递的（``Qt.QueuedConnection``），这才是对的做法。

    ``work()`` 的返回值原样进 ``done``；抛异常时发 ``(None, "错误文案")``。
    """

    done = Signal(object, str)  # result, error

    def __init__(self, work, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._work = work
        self._alive = True

    def start(self) -> None:
        threading.Thread(target=self._run, name="preview-task", daemon=True).start()

    def cancel(self) -> None:
        """窗口关了 / 用户翻页了：结果丢掉，别再 emit（对象可能已经销毁）。"""
        self._alive = False

    def _run(self) -> None:
        error = ""
        try:
            result = self._work()
        except Exception as exc:  # pragma: no cover - 交给界面显示
            result, error = None, str(exc)
        if not self._alive:
            return
        try:
            self.done.emit(result, error)
        except RuntimeError:  # pragma: no cover - 接收方已被销毁
            pass


class ImageCanvas(QLabel):
    """图片画布：滚轮以鼠标为中心缩放、拖拽平移、适应窗口不超过 100%。

    为什么不是让 ``QLabel`` 自己缩放：用户要的是"鼠标指哪儿放大哪儿"和"按住能
    拖"，这两件事都需要自己维护"缩放比 + 偏移"。这里仍然**继承 QLabel**
    （``pixmap()`` 返回当前真正画出来的那张图），外部按老方式读它是安全的。
    """

    toggle_requested = Signal()
    #: 缩放比变了（1.0 = 原始像素）
    zoom_changed = Signal(float)

    MIN_SCALE = 0.02
    MAX_SCALE = 8.0

    def __init__(self, source: QPixmap | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("PreviewCanvas")
        self.setAlignment(Qt.AlignCenter)
        self.setMinimumSize(160, 120)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setScaledContents(False)
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setCursor(Qt.OpenHandCursor)

        self._source = source if source is not None else QPixmap()
        self._scale = 1.0
        self._offset = QPointF(0.0, 0.0)
        self._fit_mode = True
        self._shown = QPixmap()
        self._render_key: tuple | None = None
        self._press_at: QPoint | None = None
        self._press_offset = QPointF()
        self._dragged = False
        self.fit()

    # -- geometry ----------------------------------------------------------

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt naming
        """A sensible default size; never the image's own size.

        返回原图大小会把窗口顶大（QLabel 的默认行为），那样"适应窗口"永远
        算不对。
        """
        return QSize(600, 420)

    def minimumSizeHint(self) -> QSize:  # noqa: N802 - Qt naming
        return QSize(160, 120)

    def source_size(self) -> QSize:
        return self._source.size()

    def scale_factor(self) -> float:
        return self._scale

    def is_fit(self) -> bool:
        return self._fit_mode

    def available(self) -> QSize:
        return QSize(
            max(32, self.width() - 2 * CANVAS_PAD),
            max(32, self.height() - 2 * CANVAS_PAD),
        )

    def fit_scale(self) -> float:
        if self._source.isNull():
            return 1.0
        area = self.available()
        ratio = min(
            area.width() / max(1, self._source.width()),
            area.height() / max(1, self._source.height()),
        )
        # 适应窗口**不放大**：一张 32×32 的图标铺满整屏只会糊。
        return max(self.MIN_SCALE, min(1.0, ratio))

    # -- content -----------------------------------------------------------

    def set_source(self, pixmap: QPixmap) -> None:
        self._source = pixmap
        self._fit_mode = True
        self.fit()

    # -- modes -------------------------------------------------------------

    def fit(self) -> None:
        self._fit_mode = True
        self._scale = self.fit_scale()
        self._centre()
        self._render()

    def actual_size(self) -> None:
        self._fit_mode = False
        self._scale = 1.0
        self._centre()
        self._render()

    def toggle(self) -> None:
        if self._fit_mode:
            self.actual_size()
        else:
            self.fit()

    def zoom_by(self, factor: float, anchor: QPointF | None = None) -> None:
        if self._source.isNull():
            return
        point = (
            QPointF(anchor)
            if anchor is not None
            else QPointF(self.width() / 2, self.height() / 2)
        )
        new_scale = max(self.MIN_SCALE, min(self.MAX_SCALE, self._scale * factor))
        if abs(new_scale - self._scale) < 1e-9:
            return
        # 鼠标底下那个像素在缩放前后必须待在同一位置。
        image_point = (point - self._offset) / self._scale
        self._offset = point - image_point * new_scale
        self._scale = new_scale
        self._fit_mode = False
        self._clamp_offset()
        self._render()

    def _centre(self) -> None:
        """把图摆到可视区正中间（适应窗口 / 原始大小 / 改变窗口大小时）。"""
        self._offset = QPointF(
            (self.width() - self._source.width() * self._scale) / 2,
            (self.height() - self._source.height() * self._scale) / 2,
        )

    def is_dragging(self) -> bool:
        """正在按住拖动？拖拽时要吃掉方向键，不能让 ←/→ 把图翻走。"""
        return self._press_at is not None

    def _clamp_offset(self, recentre_small: bool = False) -> None:
        """把图留在视野里。

        ``recentre_small=True``（适应窗口 / 原始大小）时，比可视区小的图居中放；
        缩放与拖拽走的是 ``False``：那种时候**锚点优先** —— 一旦"图比可视区小就
        居中"，滚轮缩放的锚点就会被居中挪走，用户看到的是"以鼠标为中心"失效。
        """
        width = self._source.width() * self._scale
        height = self._source.height() * self._scale
        margin = 32.0
        if width <= self.width():
            if recentre_small:
                self._offset.setX((self.width() - width) / 2)
            else:
                self._offset.setX(min(self.width() - width, max(0.0, self._offset.x())))
        else:
            self._offset.setX(min(self.width() - margin, max(margin - width, self._offset.x())))
        if height <= self.height():
            if recentre_small:
                self._offset.setY((self.height() - height) / 2)
            else:
                self._offset.setY(min(self.height() - height, max(0.0, self._offset.y())))
        else:
            self._offset.setY(min(self.height() - margin, max(margin - height, self._offset.y())))

    # -- painting ----------------------------------------------------------

    def _key(self) -> tuple:
        return (self.width(), self.height(), round(self._scale, 6))

    def _render(self) -> None:
        if self._source.isNull():
            self._shown = QPixmap()
            self._render_key = self._key()
            self.update()
            return
        target = QSize(
            max(1, int(round(self._source.width() * self._scale))),
            max(1, int(round(self._source.height() * self._scale))),
        )
        mode = Qt.SmoothTransformation if self._scale < 1.0 else Qt.FastTransformation
        self._shown = self._source.scaled(target, Qt.IgnoreAspectRatio, mode)
        self._render_key = self._key()
        self.zoom_changed.emit(self._scale)
        self.update()

    def pixmap(self) -> QPixmap:  # noqa: N802 - Qt naming
        """现在真正画在画布上的那张图（画布尺寸变了就立刻重算）。"""
        if self._render_key != self._key():
            self._clamp_offset(recentre_small=self._fit_mode)
            self._render()
        return self._shown

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt naming
        option = QStyleOption()
        option.initFrom(self)
        painter = QStylePainter(self)
        painter.drawPrimitive(QStyle.PE_Widget, option)
        shown = self.pixmap()
        if not shown.isNull():
            painter.drawPixmap(int(self._offset.x()), int(self._offset.y()), shown)
        painter.end()

    # -- interaction -------------------------------------------------------

    def wheelEvent(self, event) -> None:  # noqa: N802 - Qt naming
        delta = event.angleDelta().y() or event.angleDelta().x()
        if not delta:
            return
        self.zoom_by(1.2 ** (delta / 120.0), QPointF(event.position()))
        event.accept()

    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if event.button() == Qt.LeftButton:
            self._press_at = event.position().toPoint()
            self._press_offset = QPointF(self._offset)
            self._dragged = False
            self.setCursor(Qt.ClosedHandCursor)
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if self._press_at is not None and event.buttons() & Qt.LeftButton:
            delta = event.position().toPoint() - self._press_at
            if abs(delta.x()) > 3 or abs(delta.y()) > 3:
                self._dragged = True
            self._offset = self._press_offset + QPointF(delta)
            self._clamp_offset()
            self.update()
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if event.button() == Qt.LeftButton:
            self.setCursor(Qt.OpenHandCursor)
            was_press = self._press_at is not None
            self._press_at = None
            # 没拖动 = 点了一下：在"适应窗口 / 原始大小"之间切换（规格里就这一下）。
            if was_press and not self._dragged:
                self.toggle_requested.emit()
        super().mouseReleaseEvent(event)

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt naming
        super().resizeEvent(event)
        if self._fit_mode:
            self._scale = self.fit_scale()
            self._centre()
        else:
            self._clamp_offset()
        self._render()
        if hasattr(self, "zoom_changed"):
            self.zoom_changed.emit(self._scale)


class MediaCanvas(QLabel):
    """视频/音频的封面画布：中间压一个「▶ 播放」按钮。

    没有 FFmpeg 时给不出真封面帧 —— 那就画一个大类型图标，并在下面那行写清
    "会用哪个播放器"，而不是给一个按下去没反应的按钮。
    """

    def __init__(self, kind: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("PreviewCanvas")
        self.setAlignment(Qt.AlignCenter)
        self.setMinimumHeight(240)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.kind = kind
        self._poster: QPixmap | None = None
        self.play_button = QPushButton("▶  播放", self)
        self.play_button.setObjectName("Primary")
        self.play_button.setCursor(Qt.PointingHandCursor)
        self.play_button.setMinimumHeight(theme.CONTROL["lg"])

    def set_poster(self, pixmap: QPixmap) -> None:
        if pixmap.isNull():
            return
        self._poster = pixmap
        self._rescale()

    def _rescale(self) -> None:
        if self._poster is None or self._poster.isNull():
            return
        area = QSize(
            max(64, self.width() - 2 * CANVAS_PAD), max(64, self.height() - 2 * CANVAS_PAD)
        )
        self.setPixmap(self._poster.scaled(area, Qt.KeepAspectRatio, Qt.SmoothTransformation))

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt naming
        super().resizeEvent(event)
        self._rescale()
        hint = self.play_button.sizeHint()
        self.play_button.move(
            max(0, (self.width() - hint.width()) // 2),
            max(0, (self.height() - hint.height()) // 2),
        )
        self.play_button.raise_()


class FilePreviewDialog(QDialog):
    """预览窗口：标题是文件名，工具栏管缩放与翻页，内容按类型给。"""

    def __init__(
        self,
        path: str,
        name: str = "",
        parent: QWidget | None = None,
        data_dir: str = "",
        gallery: list[str] | None = None,
        index: int = 0,
    ) -> None:
        super().__init__(parent)
        self._data_dir = data_dir
        self.path = str(path)
        self.name = name or os.path.basename(self.path) or self.path
        self.kind = preview_kind(self.path)
        self._decode_token = 0
        self._folder_stats: tuple[int, int, int] | None = None
        self._folder_label: QLabel | None = None
        self._folder_hint: QLabel | None = None
        self._gallery: list[str] = []
        self._index = 0
        self._arrow_shortcuts: list[QShortcut] = []  # 兼容旧字段名，方向键现在在 shortcuts 里
        #: 正在跑的后台任务（大图解码 / 文件夹统计）—— 关窗时要取消。
        self._decode_task: BackgroundTask | None = None
        self._folder_task: BackgroundTask | None = None
        self.body: QWidget | None = None

        self.setWindowTitle(f"预览 · {self.name}")
        self.resize(880, 660)
        self.setMinimumSize(420, 320)
        self.setFocusPolicy(Qt.StrongFocus)

        root = QVBoxLayout(self)
        root.setContentsMargins(
            theme.SPACE["lg"], theme.SPACE["md"], theme.SPACE["lg"], theme.SPACE["md"]
        )
        root.setSpacing(theme.SPACE["md"])

        root.addWidget(self._build_toolbar())

        self.body_host = QWidget()
        self._body_layout = QVBoxLayout(self.body_host)
        self._body_layout.setContentsMargins(0, 0, 0, 0)
        self._body_layout.setSpacing(0)
        root.addWidget(self.body_host, 1)

        self.facts_label = QLabel("")
        self.facts_label.setObjectName("Subtitle")
        self.facts_label.setWordWrap(True)
        self.facts_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        root.addWidget(self.facts_label)

        self._build_body_for(self.path)
        self.set_gallery(gallery if gallery is not None else sibling_files(self.path), index)

        # 键盘（宪章 §6）：Esc 关闭、←/→ 上一张/下一张、Ctrl+S 另存为、
        # Ctrl+0 适应窗口、Ctrl+1 原始大小。全部是**真的 QShortcut** —— 按钮上
        # 写个箭头不算绑定（键盘用户按下去得有反应）。
        self.shortcuts: dict[str, QShortcut] = {
            "escape": QShortcut(QKeySequence(Qt.Key_Escape), self, activated=self.close),
            "prev": QShortcut(QKeySequence(Qt.Key_Left), self, activated=self.previous_file),
            "next": QShortcut(QKeySequence(Qt.Key_Right), self, activated=self.next_file),
            # 文本/二进制模式下 ←/→ 留给光标，翻页走 Ctrl+←/→（两种模式都能用）。
            "prev_ctrl": QShortcut(
                QKeySequence("Ctrl+Left"), self, activated=self.previous_file
            ),
            "next_ctrl": QShortcut(
                QKeySequence("Ctrl+Right"), self, activated=self.next_file
            ),
            "save": QShortcut(QKeySequence("Ctrl+S"), self, activated=self._save_as),
            "fit": QShortcut(QKeySequence("Ctrl+0"), self, activated=self.fit_window),
            "actual": QShortcut(QKeySequence("Ctrl+1"), self, activated=self.actual_size),
        }
        self._update_toolbar()
        # 工具栏一行放不下时，Qt 会把中文截断 —— 用工具栏的实际需求顶住窗口
        # 最小宽度（上限 760：比默认的 880 窄，不会反过来把窗口撑大）。
        QTimer.singleShot(0, self._sync_minimum_width)

    def _sync_minimum_width(self) -> None:
        bar = getattr(self, "toolbar", None)
        if bar is None:
            return
        needed = bar.sizeHint().width() + 2 * theme.SPACE["lg"]
        if needed <= self.minimumWidth():
            return
        self.setMinimumWidth(min(760, needed))

    # -- toolbar -----------------------------------------------------------

    def _build_toolbar(self) -> QWidget:
        """工具栏：缩放± / 适应窗口 / 原始大小 / 上一张 / 下一张 / 另存为 / 打开 / 在文件夹中显示。

        图标只用在**一眼就懂**的两个地方（缩放±、上一张/下一张），其余是纯文字：
        9 个中文按钮全写成文字的话，880px 的窗口放不下，布局会把中文挤成
        "适应窗["（半截字比没有图标难看得多）。每个按钮都按字数给了最小宽度。
        """
        bar = QFrame()
        bar.setObjectName("Toolbar")
        row = QHBoxLayout(bar)
        row.setContentsMargins(
            theme.SPACE["sm"], theme.SPACE["sm"], theme.SPACE["sm"], theme.SPACE["sm"]
        )
        row.setSpacing(theme.SPACE["xs"])

        self.zoom_out_button = self._tool_button(
            "", "zoom-out", "缩小（以鼠标位置为中心）", lambda: self.zoom(1 / 1.25)
        )
        row.addWidget(self.zoom_out_button)
        self.zoom_in_button = self._tool_button(
            "", "zoom-in", "放大（以鼠标位置为中心）", lambda: self.zoom(1.25)
        )
        row.addWidget(self.zoom_in_button)
        # 「适应窗口 / 原始大小」是一对**互斥的模式**：做成可选中的药丸，
        # 当前生效的那一个亮着（宪章 §6；不然用户在两张图之间看不出现在是哪种模式）。
        self.fit_button = self._tool_button(
            "适应窗口", "", "缩到窗口以内（不放大超过 100%，Ctrl+0）", self.fit_window,
            style="FilterChip",
        )
        self.fit_button.setCheckable(True)
        self.fit_button.setChecked(True)
        row.addWidget(self.fit_button)
        self.actual_button = self._tool_button(
            "原始大小", "", "按原始像素显示（Ctrl+1）", self.actual_size, style="FilterChip"
        )
        self.actual_button.setCheckable(True)
        row.addWidget(self.actual_button)
        self.mode_group = QButtonGroup(self)
        self.mode_group.setExclusive(True)
        self.mode_group.addButton(self.fit_button)
        self.mode_group.addButton(self.actual_button)

        row.addWidget(self._separator())

        self.prev_button = self._tool_button("", "back", "上一张（←）", self.previous_file)
        row.addWidget(self.prev_button)
        self.next_button = self._tool_button(
            "", "chevron-right", "下一张（→）", self.next_file
        )
        row.addWidget(self.next_button)

        row.addStretch(1)

        self.save_button = self._tool_button(
            "另存为", "transfer", "另存为…（Ctrl+S）", self._save_as
        )
        row.addWidget(self.save_button)
        self.open_button = self._tool_button(
            "打开", "file", "用本机默认程序打开这个文件", self._open_external
        )
        row.addWidget(self.open_button)
        self.reveal_button = self._tool_button(
            "在文件夹中显示", "folder", "在文件管理器里定位这个文件", self._reveal
        )
        row.addWidget(self.reveal_button)
        close = QPushButton("关闭")
        close.setObjectName("Ghost")
        close.setToolTip("关闭预览（Esc）")
        close.clicked.connect(self.reject)
        row.addWidget(close)
        self.toolbar = bar
        return bar

    def _tool_button(
        self, text: str, icon_name: str, tooltip: str, handler, style: str = "Ghost"
    ) -> QPushButton:
        has_icon = bool(icon_name) and icon_name in getattr(icons, "NAMES", ())
        button = QPushButton(("  " + text) if (has_icon and text) else text)
        button.setObjectName(style)
        button.setToolTip(tooltip)
        if has_icon:
            button.setIcon(icons.icon(icon_name, theme.current_text_color(), 16))
            button.setIconSize(QSize(16, 16))
        # 按字数给宽度：中文在窄按钮里会被布局截成半截字（"适应窗["）。
        metrics = button.fontMetrics()
        needed = metrics.horizontalAdvance(button.text()) + 2 * theme.SPACE["md"]
        if has_icon:
            needed += 20
        button.setMinimumWidth(max(theme.CONTROL["md"], needed))
        button.setMinimumHeight(theme.CONTROL["md"])
        button.clicked.connect(lambda _checked=False: handler())
        return button

    def _separator(self) -> QFrame:
        line = QFrame()
        line.setObjectName("VLine")
        line.setFrameShape(QFrame.VLine)
        line.setFixedWidth(1)
        line.setMinimumHeight(20)
        return line

    # -- body --------------------------------------------------------------

    def _build_body_for(self, path: str) -> QWidget:
        """换内容：就地替换 body，不关窗、不闪（翻页走的就是这里）。"""
        self.kind = preview_kind(path)
        widget = self._build_body()
        if self.body is not None:
            self._body_layout.removeWidget(self.body)
            self.body.hide()
            self.body.setParent(None)
            self.body.deleteLater()
        self._body_layout.addWidget(widget)
        self.body = widget
        self.facts_label.setText(self._facts_label_text())
        self._update_toolbar()
        self._install_arrows()
        return widget

    def _build_body(self) -> QWidget:
        if self.kind == "folder":
            return self._folder_body()
        if self.kind == "image":
            return self._image_body()
        if self.kind in ("video", "audio"):
            return self._media_body()
        if self.kind == "text":
            return self._text_body()
        return self._binary_body()

    # -- 各类内容 ----------------------------------------------------------

    def _folder_body(self) -> QWidget:
        box = QFrame()
        box.setObjectName("PreviewEmpty")
        layout = QVBoxLayout(box)
        layout.setAlignment(Qt.AlignCenter)
        layout.setSpacing(theme.SPACE["md"])
        mark = QLabel()
        mark.setPixmap(icons.icon("folder", theme.current_text_color(), 44).pixmap(44, 44))
        mark.setAlignment(Qt.AlignCenter)
        layout.addWidget(mark)
        self._folder_label = QLabel("正在统计…")
        self._folder_label.setObjectName("EmptyTitle")
        self._folder_label.setAlignment(Qt.AlignCenter)
        layout.addWidget(self._folder_label)
        self._folder_hint = QLabel("整包发过去之后，对方看到的是同样的目录结构。")
        self._folder_hint.setObjectName("EmptyHint")
        self._folder_hint.setAlignment(Qt.AlignCenter)
        self._folder_hint.setWordWrap(True)
        layout.addWidget(self._folder_hint)
        # 目录很大时 os.walk 会走很久：放线程里数，界面先显示"正在统计…"。
        self._start_folder_count()
        return box

    def _start_folder_count(self) -> None:
        folder = self.path
        task = BackgroundTask(lambda: _count_tree(folder), self)
        task.done.connect(lambda stats, error, f=folder: self._folder_counted(f, stats, error))
        self._folder_task = task
        task.start()

    def _folder_counted(self, folder: str, stats, error: str = "") -> None:
        if self.path != folder:
            return  # 用户已经翻到别的文件了
        if error or not stats:
            if self._folder_label is not None:
                self._folder_label.setText("数不出来（没有读取权限？）")
            return
        self._folder_stats = tuple(stats)
        files, folders, total = self._folder_stats
        if self._folder_label is not None:
            self._folder_label.setText(self._folder_sentence(files, folders))
        if self._folder_hint is not None:
            self._folder_hint.setText(
                f"合计 {human_bytes(total)}。整包发过去之后，对方看到的是同样的目录结构。"
            )
        self.facts_label.setText(self._facts_label_text())

    @staticmethod
    def _folder_sentence(files: int, folders: int) -> str:
        return f"文件夹里有 {files} 个文件" + (f"、{folders} 个子文件夹" if folders else "")

    # ---- 图片 ----

    def _image_body(self) -> QWidget:
        # 画布本身就是 body（``ImageCanvas`` 继承 ``QLabel``）：外部按
        # ``dialog.body.pixmap()`` 读"现在画的是什么"一直是安全的。
        canvas = ImageCanvas()
        canvas.toggle_requested.connect(self.toggle_zoom)
        canvas.zoom_changed.connect(lambda _scale: self._update_facts())
        self._canvas = canvas
        self._load_image(canvas, self.path)
        return canvas

    def _load_image(self, canvas: ImageCanvas, path: str) -> None:
        """解码图片；大图放工作线程（打开期间不卡主窗口超过 100ms）。"""
        try:
            size_bytes = os.path.getsize(path)
        except OSError:
            size_bytes = 0
        dimensions = QImageReader(path).size()
        heavy = size_bytes > ASYNC_DECODE_BYTES or (
            dimensions.isValid() and dimensions.width() * dimensions.height() > ASYNC_PIXELS
        )
        if heavy:
            canvas.setText("正在解码这张大图…（后台处理，窗口不会卡住）")
            self._decode_token += 1
            token = self._decode_token
            task = BackgroundTask(self._decode_image(path), self)
            task.done.connect(
                lambda image, error, c=canvas, t=token: self._image_ready(c, t, image, error)
            )
            self._decode_task = task
            task.start()
            return
        pixmap = QPixmap(path)
        self._apply_pixmap(canvas, pixmap)

    @staticmethod
    def _decode_image(path: str):
        """工作线程里干的活：只解码成 ``QImage``（``QPixmap`` 只能在 GUI 线程造）。"""

        def work() -> QImage:
            image = QImage()
            image.load(path)
            return image

        return work

    def _image_ready(
        self, canvas: ImageCanvas, token: int, image: QImage | None, error: str = ""
    ) -> None:
        if token != self._decode_token or canvas is not getattr(self, "_canvas", None):
            return  # 用户已经翻到下一张了
        if error or image is None:
            canvas.setText(f"这张图片打不开：{error or '解码失败'}")
            return
        self._apply_pixmap(canvas, QPixmap.fromImage(image))

    def _apply_pixmap(self, canvas: ImageCanvas, pixmap: QPixmap) -> None:
        if pixmap.isNull():
            canvas.setText(
                "这张图片打不开（文件可能坏了，或者格式系统不认识）。\n"
                "可以用「打开」交给系统的看图程序试试。"
            )
            self._update_facts()
            return
        canvas.setText("")
        canvas.set_source(pixmap)
        self._update_facts()
        self._update_toolbar()

    # ---- 视频 / 音频 ----

    def _media_body(self) -> QWidget:
        holder = QWidget()
        layout = QVBoxLayout(holder)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(theme.SPACE["sm"])

        canvas = MediaCanvas(self.kind)
        self._canvas = canvas
        poster = media.video_thumbnail(self.path, self._data_dir) if self.kind == "video" else None
        shown = False
        if poster:
            pixmap = QPixmap(poster)
            if not pixmap.isNull():
                canvas.set_poster(pixmap)
                shown = True
        if not shown:
            icon_name = "film" if self.kind == "video" else "music"
            canvas.setPixmap(icons.icon(icon_name, theme.current_text_color(), 64).pixmap(64, 64))
        canvas.play_button.clicked.connect(self._play)
        layout.addWidget(canvas, 1)

        if self.kind == "video":
            note = (
                "有内置 FFmpeg：点「▶ 播放」会调用自带的 ffplay 播放（PySide6-Essentials 里没有 "
                "QtMultimedia，窗口内放不了视频）。"
                if media.play_command(self.path, self._data_dir)
                else "点「▶ 播放」交给系统默认播放器（绿色包精简掉了 Qt 多媒体模块，窗口内不能直接放）。"
            )
            if not shown:
                note += " 这台机器上没有 FFmpeg，所以给不出封面帧。"
        else:
            note = (
                "有内置 FFmpeg：点「▶ 播放」会调用自带的 ffplay 播放。"
                if media.play_command(self.path, self._data_dir)
                else "点「▶ 播放」交给系统默认播放器。"
            )
        hint = QLabel(note)
        hint.setObjectName("Subtitle")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        return holder

    # ---- 文本 ----

    def _text_body(self) -> QWidget:
        text, truncated = read_text_preview(self.path)
        view = QPlainTextEdit()
        view.setObjectName("PreviewText")
        view.setReadOnly(True)
        view.setPlainText(text)
        view.setLineWrapMode(QPlainTextEdit.NoWrap)
        font = QFont("monospace")
        font.setStyleHint(QFont.Monospace)
        font.setPointSize(10)
        view.setFont(font)
        view.setTextInteractionFlags(Qt.TextSelectableByMouse | Qt.TextSelectableByKeyboard)
        view.moveCursor(QTextCursor.Start)
        if truncated:
            view.appendPlainText(
                f"\n…（文件比 {human_bytes(TEXT_PREVIEW_BYTES)} 大，这里只显示开头一段；"
                "要看全文请点「打开」）"
            )
        return view

    # ---- 二进制 ----

    def _binary_body(self) -> QWidget:
        box = QFrame()
        box.setObjectName("PreviewEmpty")
        layout = QVBoxLayout(box)
        layout.setAlignment(Qt.AlignCenter)
        layout.setSpacing(theme.SPACE["md"])
        mark = QLabel()
        mark.setPixmap(
            icons.icon(tile_icon_name(self.path), theme.current_text_color(), 44).pixmap(44, 44)
        )
        mark.setAlignment(Qt.AlignCenter)
        layout.addWidget(mark)
        headline = QLabel("这类文件没有可预览的内容")
        headline.setObjectName("EmptyTitle")
        headline.setAlignment(Qt.AlignCenter)
        layout.addWidget(headline)
        hint = QLabel(
            f"{self.name}\n{human_bytes(self._size_bytes())}\n\n"
            "二进制文件在界面里看不出内容，所以这里不装作能看。\n"
            "用工具栏上的「打开」交给本机对应的程序，或者「在文件夹中显示」找到它。"
        )
        hint.setObjectName("EmptyHint")
        hint.setAlignment(Qt.AlignCenter)
        hint.setWordWrap(True)
        layout.addWidget(hint)
        return box

    # -- 事实 / 状态 -------------------------------------------------------

    def _size_bytes(self) -> int:
        try:
            return os.path.getsize(self.path)
        except OSError:
            return 0

    def _facts(self) -> str:
        """这一份文件"是什么"的那行事实。

        文件夹的统计**同步**算（外部/测试问的时候必须给得出数）；界面上显示的是
        后台线程算出来的那份，先写「正在统计…」。
        """
        parts = [self._type_label()]
        if self.kind == "folder":
            files, folders, total = self._folder_stats or _count_tree(self.path)
            self._folder_stats = (files, folders, total)
            parts.append(f"{files} 个文件" + (f"、{folders} 个子文件夹" if folders else ""))
            parts.append(human_bytes(total))
        else:
            parts.append(human_bytes(self._size_bytes()))
        if self.kind in ("video", "audio"):
            duration = duration_text(self.path, self._data_dir)
            if duration:
                parts.append(duration)
        if self.kind == "image":
            canvas = getattr(self, "_canvas", None)
            if isinstance(canvas, ImageCanvas) and canvas.source_size().isValid():
                source = canvas.source_size()
                parts.append(f"{source.width()}×{source.height()}")
        parts.append(self.path)
        return "  ·  ".join(parts)

    def _facts_label_text(self) -> str:
        """界面那行：文件夹还没数完时先说"正在统计…"，不在 UI 线程走 os.walk。"""
        if self.kind == "folder" and self._folder_stats is None:
            return f"文件夹  ·  {self.path}"
        return self._facts()

    def _update_facts(self) -> None:
        if not hasattr(self, "facts_label"):
            return
        text = self._facts_label_text()
        canvas = getattr(self, "_canvas", None)
        if self.kind == "image" and isinstance(canvas, ImageCanvas):
            percent = int(round(canvas.scale_factor() * 100))
            mode = "适应窗口" if canvas.is_fit() else "原始大小"
            text = f"{mode} {percent}%  ·  " + text
        self.facts_label.setText(text)

    def _type_label(self) -> str:
        return {
            "image": "图片",
            "video": "视频",
            "audio": "音频",
            "text": "文本",
            "binary": "二进制文件",
            "folder": "文件夹",
        }.get(self.kind, "文件")

    # -- 工具栏状态 --------------------------------------------------------

    def _update_toolbar(self) -> None:
        is_image = self.kind == "image"
        for button in (
            getattr(self, "zoom_in_button", None),
            getattr(self, "zoom_out_button", None),
            getattr(self, "fit_button", None),
            getattr(self, "actual_button", None),
        ):
            if button is not None:
                button.setEnabled(is_image)
        # 模式药丸：亮着的那一个就是当前模式（适应窗口 / 原始大小）。
        canvas = getattr(self, "_canvas", None)
        if isinstance(canvas, ImageCanvas):
            self.fit_button.setChecked(canvas.is_fit())
            self.actual_button.setChecked(not canvas.is_fit())
        has_many = len(self._gallery) > 1
        if getattr(self, "prev_button", None) is not None:
            self.prev_button.setEnabled(is_image and has_many)
            self.next_button.setEnabled(is_image and has_many)
        shortcuts = getattr(self, "shortcuts", {})
        for key in ("prev", "next"):
            shortcut = shortcuts.get(key)
            if shortcut is not None:
                # ←/→ 只在图片上生效：文本视图里它要留给光标。
                shortcut.setEnabled(is_image and has_many)
        for key in ("prev_ctrl", "next_ctrl"):
            shortcut = shortcuts.get(key)
            if shortcut is not None:
                shortcut.setEnabled(has_many)
        exists = os.path.exists(self.path)
        for name in ("save_button", "open_button", "reveal_button"):
            button = getattr(self, name, None)
            if button is not None:
                button.setEnabled(exists)

    def _install_arrows(self) -> None:
        """方向键的启用条件跟着内容走（真正的绑定在 ``self.shortcuts`` 里）。

        选择与理由（Lead 问过）：
        * **图片**：`←/→` 翻页；正在拖拽平移时 ``previous_file`` 会直接返回，
          拖拽优先（用户按住鼠标拖的时候按键不该换图）。
        * **文本 / 二进制 / 视频**：`←/→` 留给文本光标（文本框要能左右移光标），
          翻页改用 `Ctrl+←/Ctrl+→` —— 键盘用户在任何模式下都能翻页。
        """
        self._update_toolbar()

    # -- 翻页 --------------------------------------------------------------

    def set_gallery(self, paths: list[str], index: int = 0) -> None:
        """同一目录里能一起翻看的文件（上一张 / 下一张）。"""
        self._gallery = [str(p) for p in (paths or []) if str(p)] or [self.path]
        target = os.path.abspath(self.path)
        positions = [
            position
            for position, item in enumerate(self._gallery)
            if os.path.abspath(item) == target
        ]
        self._index = positions[0] if positions else max(0, min(index, len(self._gallery) - 1))
        self._update_toolbar()
        self._install_arrows()

    def gallery_position(self) -> tuple[int, int]:
        return (self._index + 1, len(self._gallery))

    def _step(self, delta: int) -> None:
        if len(self._gallery) <= 1:
            return
        canvas = getattr(self, "_canvas", None)
        if isinstance(canvas, ImageCanvas) and canvas.is_dragging():
            return  # 正在拖拽平移：方向键让位给拖拽
        self._index = (self._index + delta) % len(self._gallery)
        self.show_file(self._gallery[self._index])

    def next_file(self) -> None:
        self._step(1)

    def previous_file(self) -> None:
        self._step(-1)

    def show_file(self, path: str) -> None:
        """就地换一个文件（翻页走这里，窗口不闪、不重开）。"""
        if not path or not os.path.exists(path):
            return
        self.path = str(path)
        self.name = os.path.basename(self.path) or self.path
        self.setWindowTitle(self.name)
        self._decode_token += 1  # 让上一张的异步解码结果作废
        for attr in ("_decode_task", "_folder_task"):
            task = getattr(self, attr, None)
            if task is not None:
                task.cancel()
                setattr(self, attr, None)
        self._folder_stats = None
        self._folder_label = None
        self._folder_hint = None
        self._build_body_for(self.path)

    # -- 动作 --------------------------------------------------------------

    def fit_window(self) -> None:
        canvas = getattr(self, "_canvas", None)
        if self.kind == "image" and isinstance(canvas, ImageCanvas):
            canvas.fit()
            self._update_facts()
            self._update_toolbar()

    def actual_size(self) -> None:
        canvas = getattr(self, "_canvas", None)
        if self.kind == "image" and isinstance(canvas, ImageCanvas):
            canvas.actual_size()
            self._update_facts()
            self._update_toolbar()

    def toggle_zoom(self) -> None:
        canvas = getattr(self, "_canvas", None)
        if self.kind == "image" and isinstance(canvas, ImageCanvas):
            canvas.toggle()
            self._update_facts()
            self._update_toolbar()

    def zoom(self, factor: float) -> None:
        canvas = getattr(self, "_canvas", None)
        if self.kind == "image" and isinstance(canvas, ImageCanvas):
            canvas.zoom_by(factor)
            self._update_facts()
            self._update_toolbar()

    def _play(self) -> None:
        """Play with the bundled player when there is one, else the system's."""
        command = media.play_command(self.path, self._data_dir)
        try:
            if command:
                creation = 0
                if os.name == "nt":  # pragma: no cover - Windows only
                    creation = getattr(subprocess, "CREATE_NO_WINDOW", 0)
                subprocess.Popen(
                    command,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=creation,
                )
            else:
                self._open_external()
        except Exception as exc:  # pragma: no cover - reported to the user
            QMessageBox.warning(self, "打不开", f"{self.name}\n{exc}")

    def _open_external(self) -> None:
        ok, message = platform_open.open_path_report(self.path)
        if not ok:
            QMessageBox.warning(self, "打不开", message or f"系统没有能打开 {self.name} 的程序。")

    def _save_as(self) -> None:
        from .widgets import notify, save_as

        target = save_as(self, self.path, self.name)
        if target:
            notify(self, f"已另存为 {target}")

    def _reveal(self) -> None:
        from .widgets import notify

        if not platform_open.reveal_in_file_manager(self.path):
            notify(self, "打不开文件管理器：找不到这个文件所在的文件夹。")

    # -- 键盘 --------------------------------------------------------------

    def keyPressEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if event.key() == Qt.Key_Escape:
            self.reject()
            return
        super().keyPressEvent(event)

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt naming
        for attr in ("_decode_task", "_folder_task"):
            task = getattr(self, attr, None)
            if task is not None:
                task.cancel()
        super().closeEvent(event)


class ImageViewer(FilePreviewDialog):
    """旧名字：全尺寸看一张图（现在就是那个统一的预览窗口）。

    ``_original`` 保留成未缩放的原图 —— 外部（测试、脚本）一直按它读。
    """

    def __init__(
        self,
        path: str,
        name: str = "",
        parent: QWidget | None = None,
        data_dir: str = "",
    ) -> None:
        self._original = QPixmap(path)
        super().__init__(path, name, parent, data_dir)


__all__ = [
    "FilePreviewDialog",
    "ImageCanvas",
    "ImageViewer",
    "MediaCanvas",
    "duration_text",
    "looks_like_text",
    "preview_kind",
    "read_text_preview",
    "sibling_files",
    "tile_icon_name",
    "tile_pixmap",
]
