"""Shared GUI helpers: background tasks, image conversion, small widgets."""

from __future__ import annotations

import traceback
from typing import Callable

import cv2
import numpy as np
from PySide6.QtCore import (QBuffer, QIODevice, QObject, QPointF, QRectF, QRunnable, QSettings, QSize, Qt,
                            QThreadPool, Signal)
from PySide6.QtGui import QColor, QIcon, QImage, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import (QFileDialog, QFrame, QHBoxLayout, QLabel, QMessageBox, QProgressBar,
                               QSizePolicy, QWidget)

from ..imageio import SUPPORTED_EXTENSIONS
from ..vault import VaultLockedError

IMAGE_FILTER = "Images (" + " ".join(f"*{e}" for e in sorted(SUPPORTED_EXTENSIONS)) + ")"


# --- background tasks ------------------------------------------------------------------------

class _Signals(QObject):
    done = Signal(object)
    failed = Signal(object)
    progress = Signal(int, int)


class Task(QRunnable):
    def __init__(self, fn: Callable, with_progress: bool = False):
        super().__init__()
        self.setAutoDelete(False)
        self.fn = fn
        self.with_progress = with_progress
        self.cancelled = False
        self.signals = _Signals()

    def cancel(self) -> None:
        self.cancelled = True

    def run(self) -> None:
        try:
            if self.with_progress:
                result = self.fn(lambda d, t: self.signals.progress.emit(d, t), lambda: self.cancelled)
            else:
                result = self.fn()
        except Exception as exc:  # delivered to the GUI thread
            exc.traceback_text = traceback.format_exc()
            self.signals.failed.emit(exc)
            return
        self.signals.done.emit(result)


class Runner(QObject):
    """Runs vault / engine work one task at a time off the GUI thread."""

    busy_changed = Signal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.pool = QThreadPool(self)
        self.pool.setMaxThreadCount(1)
        self._tasks: set[Task] = set()
        self.generation = 0

    def invalidate(self) -> None:
        """Results of tasks submitted before this call are dropped (used when the app locks)."""
        self.generation += 1

    def submit(self, fn: Callable, on_done: Callable | None = None, on_error: Callable | None = None,
               on_progress: Callable | None = None, with_progress: bool = False) -> Task:
        task = Task(fn, with_progress)
        gen = self.generation
        self._tasks.add(task)

        def deliver_done(result):
            self._finished(task)
            if gen == self.generation and on_done:
                on_done(result)

        def deliver_error(exc):
            self._finished(task)
            if gen == self.generation:
                (on_error or default_error_handler)(exc)

        def deliver_progress(done, total):
            if gen == self.generation and on_progress:
                on_progress(done, total)

        task.signals.done.connect(deliver_done)
        task.signals.failed.connect(deliver_error)
        task.signals.progress.connect(deliver_progress)
        self.pool.start(task)
        self.busy_changed.emit(True)
        return task

    def _finished(self, task: Task) -> None:
        self._tasks.discard(task)
        if not self._tasks:
            self.busy_changed.emit(False)

    @property
    def busy(self) -> bool:
        return bool(self._tasks)

    def cancel_all_and_wait(self, timeout_ms: int = 15000) -> None:
        for t in list(self._tasks):
            t.cancel()
        self.pool.waitForDone(timeout_ms)


def default_error_handler(exc: Exception) -> None:
    if isinstance(exc, VaultLockedError):
        return                                   # the app was locked while the task ran
    QMessageBox.warning(None, "FaceDictionary", str(exc) or exc.__class__.__name__)


# --- images -----------------------------------------------------------------------------------

def bgr_to_pixmap(bgr: np.ndarray, max_side: int | None = None) -> QPixmap:
    if max_side and max(bgr.shape[:2]) > max_side:
        s = max_side / max(bgr.shape[:2])
        bgr = cv2.resize(bgr, (round(bgr.shape[1] * s), round(bgr.shape[0] * s)), interpolation=cv2.INTER_AREA)
    bgr = np.ascontiguousarray(bgr)
    h, w = bgr.shape[:2]
    img = QImage(bgr.data, w, h, bgr.strides[0], QImage.Format.Format_BGR888)
    return QPixmap.fromImage(img.copy())


def bytes_to_pixmap(data: bytes | None) -> QPixmap:
    pm = QPixmap()
    if data:
        pm.loadFromData(data)
    return pm


def qimage_to_png_bytes(image: QImage) -> bytes:
    buf = QBuffer()
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    image.save(buf, "PNG")
    return bytes(buf.data())


def placeholder_pixmap(size: int, text: str = "?") -> QPixmap:
    pm = QPixmap(size, size)
    pm.fill(QColor(128, 128, 128, 60))
    p = QPainter(pm)
    p.setPen(QColor(128, 128, 128))
    f = p.font()
    f.setPointSize(max(10, size // 4))
    p.setFont(f)
    p.drawText(pm.rect(), Qt.AlignmentFlag.AlignCenter, text)
    p.end()
    return pm


def rounded(pm: QPixmap, radius: int = 8) -> QPixmap:
    if pm.isNull():
        return pm
    out = QPixmap(pm.size())
    out.fill(Qt.GlobalColor.transparent)
    p = QPainter(out)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    path = QPainterPath()
    path.addRoundedRect(0, 0, pm.width(), pm.height(), radius, radius)
    p.setClipPath(path)
    p.drawPixmap(0, 0, pm)
    p.end()
    return out


# --- widgets ----------------------------------------------------------------------------------

class ClickableLabel(QLabel):
    """A label that behaves like a link."""

    clicked = Signal()

    def __init__(self, text: str = "", tooltip: str = "", parent=None):
        super().__init__(text, parent)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        if tooltip:
            self.setToolTip(tooltip)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()


class ElidedLabel(QLabel):
    """One-line text that shortens itself with “…” instead of being cut off."""

    def __init__(self, text: str = "", parent=None):
        super().__init__(parent)
        self.setObjectName("muted")
        self.full_text = text
        self.setMinimumWidth(40)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.setToolTip(text)
        self._update()

    def setText(self, text: str) -> None:
        self.full_text = text
        self.setToolTip(text)
        self._update()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._update()

    def _update(self) -> None:
        from PySide6.QtGui import QFontMetrics
        super().setText(QFontMetrics(self.font()).elidedText(self.full_text, Qt.TextElideMode.ElideRight,
                                                            max(40, self.contentsRect().width() - 8)))


class ScoreBar(QProgressBar):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setRange(0, 100)
        self.setTextVisible(True)
        self.setFixedHeight(16)

    def set_score(self, score: float, low_threshold: float) -> None:
        self.setValue(max(0, min(100, round(score * 100))))
        self.setFormat(f"{score:.3f}")
        color = "#3a9d5d" if score >= low_threshold + 0.15 else ("#d49a22" if score >= low_threshold else "#c9483c")
        self.setStyleSheet(f"QProgressBar {{ border: 1px solid palette(mid); border-radius: 5px; text-align: center; "
                           f"font-size: 11px; }} QProgressBar::chunk {{ background: {color}; border-radius: 4px; }}")


# --- one set of thin monochrome line icons, drawn on a 24-unit grid (in the manner of Lucide) ---------

def _path(*points) -> QPainterPath:
    path = QPainterPath()
    path.moveTo(*points[0])
    for pt in points[1:]:
        path.lineTo(*pt)
    return path


def _draw_icon(p: QPainter, name: str) -> None:
    if name in ("plus", "minus"):
        p.drawLine(QPointF(5, 12), QPointF(19, 12))
        if name == "plus":
            p.drawLine(QPointF(12, 5), QPointF(12, 19))
    elif name == "ban":
        p.drawEllipse(QPointF(12, 12), 9, 9)
        p.drawLine(QPointF(5.6, 5.6), QPointF(18.4, 18.4))
    elif name == "trash":
        p.drawLine(QPointF(3, 6), QPointF(21, 6))
        p.drawPath(_path((5.5, 6), (6.5, 21), (17.5, 21), (18.5, 6)))
        p.drawPath(_path((9, 6), (9, 3), (15, 3), (15, 6)))
        p.drawLine(QPointF(10, 10.5), QPointF(10, 16.5))
        p.drawLine(QPointF(14, 10.5), QPointF(14, 16.5))
    elif name == "settings":                                   # two sliders
        p.drawLine(QPointF(3, 7), QPointF(4, 7))
        p.drawLine(QPointF(10, 7), QPointF(21, 7))
        p.drawEllipse(QPointF(7, 7), 3, 3)
        p.drawLine(QPointF(3, 17), QPointF(14, 17))
        p.drawLine(QPointF(20, 17), QPointF(21, 17))
        p.drawEllipse(QPointF(17, 17), 3, 3)
    elif name == "lock":
        p.drawRoundedRect(QRectF(4.5, 11, 15, 10), 2, 2)
        shackle = QPainterPath()
        shackle.moveTo(8, 11)
        shackle.lineTo(8, 7)
        shackle.arcTo(QRectF(8, 3, 8, 8), 180, -180)
        shackle.lineTo(16, 11)
        p.drawPath(shackle)
    elif name in ("import", "export"):                         # a tray with an arrow into it, or out of it
        p.drawPath(_path((3, 15), (3, 21), (21, 21), (21, 15)))
        p.drawLine(QPointF(12, 3), QPointF(12, 15))
        if name == "import":
            p.drawPath(_path((7, 10), (12, 15), (17, 10)))
        else:
            p.drawPath(_path((7, 8), (12, 3), (17, 8)))
    else:
        raise ValueError(f"no icon called {name!r}")


_ICONS: dict[str, QIcon] = {}


def line_icon(name: str):
    """A thin line icon in the text colour of the current palette (lighter when disabled)."""
    from PySide6.QtGui import QGuiApplication, QPalette
    if name in _ICONS:
        return _ICONS[name]
    palette = QGuiApplication.palette()
    icon = QIcon()
    for mode, group in ((QIcon.Mode.Normal, QPalette.ColorGroup.Active),
                        (QIcon.Mode.Disabled, QPalette.ColorGroup.Disabled)):
        color = palette.color(group, QPalette.ColorRole.ButtonText)
        if mode == QIcon.Mode.Disabled:
            color.setAlpha(110)
        for size in (16, 18, 20, 24):
            for ratio in (1, 2, 3):
                pm = QPixmap(size * ratio, size * ratio)
                pm.setDevicePixelRatio(ratio)
                pm.fill(Qt.GlobalColor.transparent)
                p = QPainter(pm)
                p.setRenderHint(QPainter.RenderHint.Antialiasing)
                p.scale(size / 24, size / 24)
                pen = QPen(color, 2)
                pen.setCapStyle(Qt.PenCapStyle.RoundCap)
                pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
                p.setPen(pen)
                p.setBrush(Qt.BrushStyle.NoBrush)
                _draw_icon(p, name)
                p.end()
                icon.addPixmap(pm, mode)
    _ICONS[name] = icon
    return icon


def icon_button(icon: str, tooltip: str, size: int = 28, checkable: bool = False):
    """A small borderless button with a line icon; the words live in the tooltip."""
    from PySide6.QtWidgets import QToolButton
    b = QToolButton()
    b.setObjectName("icon")
    b.setIcon(line_icon(icon))
    b.setIconSize(QSize(18, 18) if size >= 26 else QSize(16, 16))
    b.setToolTip(tooltip)
    b.setFixedSize(size, size)
    b.setCheckable(checkable)
    return b


def mark_excluded(pm: QPixmap, size: int) -> QPixmap:
    """Dim a thumbnail and draw a red “no entry” sign: this photo is not used for matching."""
    out = QPixmap(pm.size())
    out.fill(Qt.GlobalColor.transparent)
    p = QPainter(out)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.drawPixmap(0, 0, pm)
    p.fillRect(out.rect(), QColor(20, 20, 20, 120))
    ratio = out.width() / max(1, size)
    d = out.width() * 0.5
    x, y = (out.width() - d) / 2, (out.height() - d) / 2
    pen = QPen(QColor(226, 76, 60), max(2.0, 4.0 * ratio))
    p.setPen(pen)
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.drawEllipse(x, y, d, d)
    offset = d * 0.146                                  # the bar of the sign, at 45 degrees
    p.drawLine(x + offset, y + d - offset, x + d - offset, y + offset)
    p.end()
    return out


def mark_cover(pm: QPixmap, size: int) -> QPixmap:
    """Frame a thumbnail: this photo is the entry's cover."""
    from PySide6.QtGui import QGuiApplication, QPalette
    out = QPixmap(pm)
    p = QPainter(out)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    width = max(2.0, 3.0 * out.width() / max(1, size))
    p.setPen(QPen(QGuiApplication.palette().color(QPalette.ColorRole.Text), width))
    p.setBrush(Qt.BrushStyle.NoBrush)
    half = width / 2
    p.drawRect(QRectF(half, half, out.width() - width, out.height() - width))
    p.end()
    return out


HEADER_HEIGHT = 38


def header_row(title: str, *widgets, size: int = 15) -> QWidget:
    """The band at the top of a column: a title on the left, buttons on the right.
    Every page uses it, so the columns of all pages start at the same height."""
    bar = QWidget()
    bar.setFixedHeight(HEADER_HEIGHT)
    lay = QHBoxLayout(bar)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(GAP)
    if title:
        lay.addWidget(heading(title, size))
    lay.addStretch(1)
    for w in widgets:
        lay.addWidget(w)
    return bar


def divider(text: str = "") -> QWidget:
    """A thin horizontal line, optionally with a small caption on the left."""
    row = QWidget()
    lay = QHBoxLayout(row)
    lay.setContentsMargins(0, 6, 0, 2)
    lay.setSpacing(8)
    if text:
        lay.addWidget(muted(text))
    line = QFrame()
    line.setFrameShape(QFrame.Shape.HLine)
    line.setFrameShadow(QFrame.Shadow.Plain)
    line.setStyleSheet("color: palette(midlight);")
    lay.addWidget(line, 1)
    return row


def add_copy_action(widget) -> None:
    """Right-click (or ⌘C) copies the selected row of a list or tree."""
    from PySide6.QtGui import QAction, QGuiApplication, QKeySequence

    action = QAction("Copy", widget)
    action.setShortcut(QKeySequence.StandardKey.Copy)
    action.setShortcutContext(Qt.ShortcutContext.WidgetShortcut)

    def copy() -> None:
        item = widget.currentItem()
        if item is None:
            return
        if hasattr(item, "columnCount"):
            text = "  ".join(item.text(c) for c in range(item.columnCount()) if item.text(c))
        else:
            text = item.text()
        QGuiApplication.clipboard().setText(text.replace("\n", " — "))

    action.triggered.connect(copy)
    widget.addAction(action)
    widget.setContextMenuPolicy(Qt.ContextMenuPolicy.ActionsContextMenu)


NOT_SELECTABLE = ("ClickableLabel", "ElidedLabel")      # a click there has its own meaning


def make_selectable(label: QLabel) -> QLabel:
    """Let the user select and copy a label's text."""
    flags = label.textInteractionFlags()
    if not flags & Qt.TextInteractionFlag.TextSelectableByMouse:
        label.setTextInteractionFlags(flags | Qt.TextInteractionFlag.TextSelectableByMouse)
    return label


def make_labels_selectable(root) -> None:
    """Apply it to every label of a window, page or dialog that was just built."""
    for label in root.findChildren(QLabel):
        if type(label).__name__ not in NOT_SELECTABLE:
            make_selectable(label)


def has_focus_in(widget, focus) -> bool:
    """The keyboard focus is on `widget` or inside it (a list's viewport, say)."""
    return focus is not None and (focus is widget or widget.isAncestorOf(focus))


def card() -> QFrame:
    f = QFrame()
    f.setObjectName("card")
    return f


def heading(text: str, size: int = 15) -> QLabel:
    lbl = QLabel(text)
    lbl.setFont(semibold(lbl.font(), size))
    return make_selectable(lbl)


def semibold(font, size: int | None = None):
    """The one emphasis the app uses: semibold, never bold."""
    from PySide6.QtGui import QFont
    f = QFont(font)
    if size is not None:
        f.setPointSize(size)
    f.setWeight(QFont.Weight.DemiBold)
    return f


PAGE_MARGIN = 24                  # around every page
GUTTER = 24                       # between the columns of a page
GAP = 8                           # between the parts of a column


def muted(text: str = "") -> QLabel:
    lbl = QLabel(text)
    lbl.setObjectName("muted")
    lbl.setWordWrap(True)
    return make_selectable(lbl)


def confirm(parent, title: str, text: str, ok_text: str = "OK", destructive: bool = False) -> bool:
    box = QMessageBox(parent)
    box.setIcon(QMessageBox.Icon.Warning if destructive else QMessageBox.Icon.Question)
    box.setWindowTitle(title)
    box.setText(text)
    box.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    ok = box.addButton(ok_text, QMessageBox.ButtonRole.DestructiveRole if destructive
                       else QMessageBox.ButtonRole.AcceptRole)
    box.addButton(QMessageBox.StandardButton.Cancel)
    box.setDefaultButton(QMessageBox.StandardButton.Cancel)
    box.exec()
    return box.clickedButton() is ok


# --- file dialogs that leave no trace ----------------------------------------------------------

def _forget_dialog_history() -> None:
    """Qt's own file dialog remembers visited folders in QSettings('QtProject'); wipe that."""
    s = QSettings(QSettings.Scope.UserScope, "QtProject")
    s.beginGroup("FileDialog")
    for key in ("history", "lastVisited"):
        s.remove(key)
    s.endGroup()
    s.sync()


def _dialog_options() -> QFileDialog.Option:
    return QFileDialog.Option.DontUseNativeDialog


_LAST_FOLDERS: dict[str, str] = {}      # where each kind of photo dialog was last used; memory only, never saved


def open_images(parent, title: str = "Choose photos", multiple: bool = True, kind: str = "photos") -> list[str]:
    """Choose photos, starting in the folder the last dialog of this `kind` ended in."""
    from pathlib import Path
    start = _LAST_FOLDERS.get(kind, "")
    if not start or not Path(start).is_dir():
        start = str(Path.home())
    try:
        if multiple:
            paths, _ = QFileDialog.getOpenFileNames(parent, title, start, IMAGE_FILTER, options=_dialog_options())
        else:
            path, _ = QFileDialog.getOpenFileName(parent, title, start, IMAGE_FILTER, options=_dialog_options())
            paths = [path] if path else []
    finally:
        _forget_dialog_history()
    if paths:
        _LAST_FOLDERS[kind] = str(Path(paths[0]).parent)
    return paths


def choose_folder(parent, title: str) -> str:
    from pathlib import Path
    try:
        return QFileDialog.getExistingDirectory(parent, title, str(Path.home()),
                                                QFileDialog.Option.ShowDirsOnly | _dialog_options())
    finally:
        _forget_dialog_history()


STYLE = """
QFrame#card { border: none; border-radius: 10px; background: palette(base); }
QFrame#dropzone { border: 2px dashed palette(mid); border-radius: 14px; background: palette(base); }
QFrame#dropzone[hover="true"] { border-color: palette(highlight); }
QLabel#muted { color: palette(placeholder-text); font-size: 12pt; }
QLabel#banner { background: #fff3cd; color: #6b4e00; border-radius: 8px; padding: 8px 10px; }
QListWidget, QTreeWidget { border: none; background: transparent; outline: 0; }
QListWidget::item, QTreeWidget::item { border: none; padding: 3px 4px; }
QListWidget::item:selected, QTreeWidget::item:selected { background: palette(highlight);
                                                          color: palette(highlighted-text); border-radius: 7px; }
QListWidget::item:selected:!active, QTreeWidget::item:selected:!active { background: palette(midlight);
                                                                          color: palette(text); }
QTreeWidget::item:selected { border-radius: 0px; }
QTreeWidget::item:selected:first { border-top-left-radius: 7px; border-bottom-left-radius: 7px; }
QTreeWidget::item:selected:last { border-top-right-radius: 7px; border-bottom-right-radius: 7px; }
QTreeWidget QHeaderView::section { background: transparent; border: none; border-bottom: 1px solid palette(midlight);
                                    padding: 4px 4px; color: palette(placeholder-text); }
QListWidget#nav { font-size: 14px; }
QListWidget#nav::item { padding: 7px 10px; margin: 1px 6px; }
QSplitter::handle { background: transparent; image: none; }
QStatusBar { background: transparent; border: none; }
QStatusBar::item { border: none; }
QToolButton#icon { border: none; background: transparent; border-radius: 6px; }
QToolButton#icon:pressed, QToolButton#icon:checked { background: rgba(127, 127, 127, 60); }
QPushButton#primary { font-weight: 600; }
QLabel[inline="true"] { padding: 1px 0px; }
QLabel[inline="true"][empty="true"] { color: palette(placeholder-text); }
"""
