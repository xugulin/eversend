"""中文界面：把 Qt 自带的翻译装上，让系统对话框也说中文。

用户报的原话是「**明明是中文的软件，选文件弹窗却是英文**」。根因不是我们没写中文
文案 —— 我们自己的控件全是中文；出问题的是 **Qt 自己的对话框**：
``QFileDialog`` 的「File name: / Files of type: / Open / Cancel / Look in」、
``QMessageBox`` 的「OK / Cancel / Yes / No」、``QLineEdit`` 右键菜单的
「Cut / Copy / Paste / Select All」、``QHeaderView``、``QColorDialog`` ……
这些字符串都在 ``qtbase_zh_CN.qm`` 里，而项目从来没有 ``QTranslator``，
所以 Qt 一律回落到英文源串。

这个模块做四件事：

1. 把 ``QLocale`` 默认值设成 ``zh_CN``（影响日期/数字格式与 Qt 自己挑翻译的偏好）；
2. 从 PySide6 的 ``Qt/translations`` 目录装 ``qt_zh_CN`` 与 ``qtbase_zh_CN``；
3. 绿色便携包里 Qt 的翻译目录和解释器不在同一个位置，所以再扫几个候选目录
   （``EVERSEND_QT_TRANSLATIONS``、可执行文件同级、``<root>/runtime``）；
4. **装不上就退回自绘对话框**：给 ``QApplication`` 设 ``AA_DontUseNativeDialogs``，
   免得 Linux 上弹出一个跟随系统 locale 的原生 GTK 对话框 —— 那会绕过我们的翻译，
   在英文系统上就又是一次"中文软件弹英文框"。

诊断信息通过 :func:`describe` 暴露，设置页的"诊断"卡片和验收测试都用它。
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field

#: 要装的翻译，按优先级从左到右。``qt_zh_CN`` 是元翻译（含 qtbase 等所有子模块），
#: ``qtbase_zh_CN`` 是兜底 —— 只装了 qtbase 的精简发行版里只有后者。
_TRANSLATION_NAMES = ("qt_zh_CN", "qtbase_zh_CN")

#: 装好的 translator 必须被引用住，否则会被 GC 掉、翻译立刻失效。
_installed: list = []


# ---------------------------------------------------------------------------
# Qt 自带目录缺的那几条
#
# ``qtbase_zh_CN.qm``（Qt 6.11）**漏译了 QFileDialog 的两个标签**：
# ``&Look in:`` 与 ``Files of &type:`` —— 而文件名标签、打开/取消按钮、文件列表的
# 列头（名称/体积/类型/修改日期）、工具栏提示、QMessageBox 全部按钮、QLineEdit 与
# QTextEdit 的右键菜单都是译好的。实测（离屏构造真对话框、遍历控件取 text()）只有
# 这两条漏网，所以这里只补这一小张表，而不是自己重写一整套翻译。
#
# 候选表**在安装时逐条探测**：装完 Qt 目录之后先问一次
# ``QCoreApplication.translate(ctx, src)``，只有它原样返回英文源串（= 确实没译）
# 才启用我们这条。这样以后换了 Qt 版本、目录补全了，我们的条目会自动让位，
# 不会用一个旧译法盖掉新译法。
# ---------------------------------------------------------------------------
_KNOWN_GAPS: tuple[tuple[str, str, str], ...] = (
    # -- QFileDialog：实测漏译的两条（其余几条是给别的 Qt 版本兜底的）------
    ("QFileDialog", "&Look in:", "位置(&L)："),
    ("QFileDialog", "Look in:", "位置："),
    ("QFileDialog", "Files of &type:", "文件类型(&T)："),
    ("QFileDialog", "Files of type:", "文件类型："),
    # -- QFileDialog：其余标签/按钮，仅在对应 Qt 版本漏译时才生效 ----------
    ("QFileDialog", "&File name:", "文件名称(&N)："),
    ("QFileDialog", "File &name:", "文件名称(&N)："),
    ("QFileDialog", "File name:", "文件名称："),
    ("QFileDialog", "Open", "打开"),
    ("QFileDialog", "Save", "保存"),
    ("QFileDialog", "Cancel", "取消"),
    ("QFileDialog", "&Open", "打开(&O)"),
    ("QFileDialog", "&Save", "保存(&S)"),
    ("QFileDialog", "&Cancel", "取消"),
    ("QFileDialog", "&Choose", "选择(&C)"),
    ("QFileDialog", "Choose", "选择"),
    ("QFileDialog", "&New Folder", "新建文件夹(&N)"),
    ("QFileDialog", "New Folder", "新建文件夹"),
    ("QFileDialog", "Create New Folder", "新建文件夹"),
    ("QFileDialog", "Delete", "删除"),
    ("QFileDialog", "Rename", "重命名"),
    ("QFileDialog", "Remove", "移除"),
    ("QFileDialog", "Show Hidden Files", "显示隐藏的文件"),
    ("QFileDialog", "Back", "后退"),
    ("QFileDialog", "Forward", "前进"),
    ("QFileDialog", "Parent Directory", "父目录"),
    ("QFileDialog", "List View", "列表视图"),
    ("QFileDialog", "Detail View", "详细视图"),
    ("QFileDialog", "Name", "名称"),
    ("QFileDialog", "Size", "大小"),
    ("QFileDialog", "Type", "类型"),
    ("QFileDialog", "Date Modified", "修改日期"),
    ("QFileDialog", "Recent Places", "最近使用的位置"),
    ("QFileDialog", "Computer", "计算机"),
    ("QFileDialog", "Desktop", "桌面"),
    ("QFileDialog", "Drives", "驱动器"),
    ("QFileDialog", "All Files (*)", "所有文件 (*)"),
)


def _make_gap_translator(mapping: dict[tuple[str, str], str]):
    """Build a ``QTranslator`` that answers only for ``mapping``.

    必须**最后安装**：Qt 查翻译时按"后装的先问"，先问我们就先命中；
    我们没命中的（绝大多数）返回空串，Qt 会继续问 Qt 自带目录，行为不变。
    """
    from PySide6.QtCore import QTranslator

    class _GapTranslator(QTranslator):
        def __init__(self, table: dict[tuple[str, str], str]) -> None:
            super().__init__()
            self._table = table

        def translate(self, context, source, disambiguation=None, n=-1):  # noqa: N802
            # **必须返回 None 而不是 ""**：Qt 判断"这个 translator 是否命中"看的是
            # 返回值**是不是 null**，空但非 null 的串会被当成"命中且译文为空"，
            # 于是 Qt 不再去问后面那个真正有译文的目录 —— 实测这样会让
            # 「打开 / 取消 / 文件名称」全部变成空白。
            hit = self._table.get((context, source))
            return hit if hit else None

    return _GapTranslator(mapping)


def active_gaps() -> list[tuple[str, str]]:
    """Which entries of ``_KNOWN_GAPS`` are actually in force (for diagnostics)."""
    return list(status.gaps)


@dataclass
class TranslationStatus:
    """What actually happened, for the diagnostics card and the test suite."""

    locale: str = ""
    searched: list[str] = field(default_factory=list)
    loaded: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    force_own_dialogs: bool = False
    #: 实际启用的补译条目 ``(context, source)`` —— 只有 Qt 目录真漏了的那些。
    gaps: list[tuple[str, str]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.loaded)


#: 最近一次 :func:`install` 的结果。
status = TranslationStatus()


def candidate_dirs() -> list[str]:
    """Everywhere a Qt translation directory could plausibly live.

    Order matters: an explicit override wins, then the running interpreter's
    own Qt (a source checkout / venv), then the portable package layout where
    Qt sits beside the bundled interpreter.
    """
    dirs: list[str] = []
    override = os.environ.get("EVERSEND_QT_TRANSLATIONS", "").strip()
    if override:
        dirs.append(override)
    try:
        import PySide6  # noqa: PLC0415

        dirs.append(os.path.join(os.path.dirname(PySide6.__file__), "Qt", "translations"))
    except Exception:  # pragma: no cover - PySide6 always present when this runs
        pass
    # 绿色包：解释器在 <root>/runtime/bin，Qt 在 <root>/runtime/lib/.../translations
    for base in {os.path.dirname(os.path.abspath(sys.executable)), os.getcwd()}:
        root = os.path.dirname(base)
        dirs.append(os.path.join(base, "translations"))
        dirs.append(os.path.join(root, "translations"))
        dirs.append(os.path.join(root, "runtime", "translations"))
        dirs.append(os.path.join(base, "PySide6", "Qt", "translations"))
        dirs.append(os.path.join(root, "lib", "PySide6", "Qt", "translations"))
    # 去重但保持顺序
    seen: set[str] = set()
    unique: list[str] = []
    for path in dirs:
        if path and path not in seen:
            seen.add(path)
            unique.append(path)
    return unique


def find_translations() -> tuple[list[str], list[str]]:
    """Return ``(dirs_that_exist, qm_files_found)``."""
    dirs = [d for d in candidate_dirs() if os.path.isdir(d)]
    found: list[str] = []
    for directory in dirs:
        for name in _TRANSLATION_NAMES:
            path = os.path.join(directory, f"{name}.qm")
            if os.path.isfile(path) and path not in found:
                found.append(path)
    return dirs, found


def install(app=None, *, force_chinese: bool = True) -> TranslationStatus:
    """Install the Chinese translations onto ``app`` (or the current instance).

    Safe to call more than once; the second call returns the same status.
    Returns a :class:`TranslationStatus` describing what was loaded, which is
    what the diagnostics card and the acceptance test read.
    """
    global status
    if _installed:
        return status

    from PySide6.QtCore import QCoreApplication, QLibraryInfo, QLocale, QTranslator

    if app is None:
        app = QCoreApplication.instance()

    result = TranslationStatus()

    if force_chinese:
        # 显式设成中文：不能指望用户机器上有 zh_CN locale，而我们是个中文软件。
        locale = QLocale(QLocale.Chinese, QLocale.China)
        QLocale.setDefault(locale)
        result.locale = locale.name()

    dirs, found = find_translations()
    # QLibraryInfo 报的是 Qt 自己认的目录；实测在部分安装方式里它指向一个空目录，
    # 所以只把它当补充而不是唯一来源。
    try:
        qt_dir = QLibraryInfo.path(QLibraryInfo.TranslationsPath)
        if qt_dir and os.path.isdir(qt_dir) and qt_dir not in dirs:
            dirs.append(qt_dir)
            for name in _TRANSLATION_NAMES:
                path = os.path.join(qt_dir, f"{name}.qm")
                if os.path.isfile(path) and path not in found:
                    found.append(path)
    except Exception:  # pragma: no cover - older/newer Qt without the enum
        pass
    result.searched = dirs

    for path in found:
        translator = QTranslator()
        if translator.load(path) and app is not None and app.installTranslator(translator):
            _installed.append(translator)
            result.loaded.append(path)

    if not result.loaded:
        result.missing = [f"{name}.qm" for name in _TRANSLATION_NAMES]

    # 补 Qt 目录漏译的那两条（见 _KNOWN_GAPS）：先探一遍"Qt 自己译没译"，
    # 只有原样返回英文源串的才启用我们的译法 —— 换 Qt 版本时自动让位。
    if result.loaded:
        table: dict[tuple[str, str], str] = {}
        for context, source, chinese in _KNOWN_GAPS:
            try:
                current = QCoreApplication.translate(context, source)
            except Exception:  # pragma: no cover
                continue
            if current == source:  # 没译 → 我们补
                table[(context, source)] = chinese
        if table:
            gap = _make_gap_translator(table)
            if app is not None and app.installTranslator(gap):
                _installed.append(gap)
                result.gaps = sorted(table)

    # 没装上 Qt 的中文翻译时，**绝不放行系统原生对话框**：原生对话框跟随操作系统
    # locale，在一个 en_US 的机器上就会弹出英文的 GTK 选文件框 —— 那正是用户报的
    # 那个 bug。用 Qt 自绘对话框至少文案是我们能控制的。
    if app is not None and not result.loaded:
        try:
            from PySide6.QtCore import Qt

            app.setAttribute(Qt.AA_DontUseNativeDialogs, True)
            result.force_own_dialogs = True
        except Exception:  # pragma: no cover
            pass

    status = result
    return result


def describe() -> str:
    """One line for the diagnostics card."""
    if status.ok:
        names = ", ".join(os.path.basename(p) for p in status.loaded)
        extra = f"，补译 {len(status.gaps)} 条" if status.gaps else ""
        return f"界面语言：简体中文（Qt 翻译：{names}{extra}）"
    return "界面语言：简体中文（Qt 自带翻译未找到，已改用自绘对话框）"


def retranslate(widget) -> None:
    """Force Qt to re-read every translated string under ``widget``.

    Qt sends a ``LanguageChange`` event to the top-level widgets when a
    translator is installed *after* they were built.  We install before the
    window exists, but tests and the "switch language" path still call this.
    """
    from PySide6.QtCore import QEvent

    if widget is None:
        return
    from PySide6.QtWidgets import QApplication

    for top in QApplication.topLevelWidgets():
        QApplication.sendEvent(top, QEvent(QEvent.LanguageChange))


__all__ = [
    "TranslationStatus",
    "candidate_dirs",
    "describe",
    "find_translations",
    "install",
    "retranslate",
    "status",
]
