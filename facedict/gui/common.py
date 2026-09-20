"""Shared GUI helpers: background tasks, image conversion, small widgets."""

from __future__ import annotations

import traceback
from typing import Callable

import cv2
import numpy as np
from PySide6.QtCore import QBuffer, QIODevice, QObject, QRunnable, QSettings, QSize, Qt, QThreadPool, Signal
from PySide6.QtGui import QColor, QImage, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import (QFileDialog, QFrame, QHBoxLayout, QLabel, QMessageBox, QProgressBar,
                               QSizePolicy, QVBoxLayout, QWidget)

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

class ImageView(QLabel):
    """Shows a pixmap scaled to fit, keeping the aspect ratio."""

    def __init__(self, min_size: QSize = QSize(200, 200), parent=None):
        super().__init__(parent)
        self._pixmap = QPixmap()
        self.setMinimumSize(min_size)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

    def set_image(self, pm: QPixmap | None) -> None:
        self._pixmap = pm if pm is not None else QPixmap()
        self._rescale()

    def clear_image(self) -> None:
        self._pixmap = QPixmap()
        super().clear()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._rescale()

    def _rescale(self) -> None:
        if self._pixmap.isNull():
            super().clear()
            return
        ratio = self.devicePixelRatioF()
        scaled = self._pixmap.scaled(self.size() * ratio, Qt.AspectRatioMode.KeepAspectRatio,
                                     Qt.TransformationMode.SmoothTransformation)
        scaled.setDevicePixelRatio(ratio)
        self.setPixmap(scaled)


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
                                                            max(40, self.width())))


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


def icon_button(symbol: str, tooltip: str, size: int = 30, checkable: bool = False):
    """A small square button with a symbol; the words live in the tooltip."""
    from PySide6.QtWidgets import QPushButton
    b = QPushButton(symbol)
    b.setToolTip(tooltip)
    b.setFixedSize(size, size)
    b.setCheckable(checkable)
    f = b.font()
    f.setPointSize(15)
    b.setFont(f)
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


HEADER_HEIGHT = 38


def header_row(title: str, *widgets, size: int = 15) -> QWidget:
    """The band at the top of a column: a title on the left, buttons on the right.
    Every page uses it, so the columns of all pages start at the same height."""
    bar = QWidget()
    bar.setFixedHeight(HEADER_HEIGHT)
    lay = QHBoxLayout(bar)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(6)
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


def card() -> QFrame:
    f = QFrame()
    f.setObjectName("card")
    return f


def heading(text: str, size: int = 15) -> QLabel:
    lbl = QLabel(text)
    f = lbl.font()
    f.setPointSize(size)
    f.setBold(True)
    lbl.setFont(f)
    return make_selectable(lbl)


def muted(text: str = "") -> QLabel:
    lbl = QLabel(text)
    lbl.setObjectName("muted")
    lbl.setWordWrap(True)
    return make_selectable(lbl)


def vbox(*widgets, margins=(0, 0, 0, 0), spacing=6) -> QVBoxLayout:
    lay = QVBoxLayout()
    lay.setContentsMargins(*margins)
    lay.setSpacing(spacing)
    for w in widgets:
        if isinstance(w, QWidget):
            lay.addWidget(w)
        else:
            lay.addLayout(w)
    return lay


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


def open_images(parent, title: str = "Choose photos", multiple: bool = True) -> list[str]:
    from pathlib import Path
    try:
        if multiple:
            paths, _ = QFileDialog.getOpenFileNames(parent, title, str(Path.home()), IMAGE_FILTER,
                                                    options=_dialog_options())
        else:
            path, _ = QFileDialog.getOpenFileName(parent, title, str(Path.home()), IMAGE_FILTER,
                                                  options=_dialog_options())
            paths = [path] if path else []
    finally:
        _forget_dialog_history()
    return paths


def choose_folder(parent, title: str) -> str:
    from pathlib import Path
    try:
        return QFileDialog.getExistingDirectory(parent, title, str(Path.home()),
                                                QFileDialog.Option.ShowDirsOnly | _dialog_options())
    finally:
        _forget_dialog_history()


STYLE = """
QFrame#card { border: 1px solid palette(midlight); border-radius: 10px; background: palette(base); }
QFrame#dropzone { border: 2px dashed palette(mid); border-radius: 14px; background: palette(base); }
QFrame#dropzone[hover="true"] { border-color: palette(highlight); }
QLabel#muted { color: palette(placeholder-text); }
QLabel#banner { background: #fff3cd; color: #6b4e00; border-radius: 8px; padding: 8px 10px; }
QListWidget#nav { border: none; background: transparent; font-size: 14px; }
QListWidget#nav::item { padding: 8px 10px; border-radius: 8px; margin: 1px 6px; }
QListWidget#nav::item:selected { background: palette(highlight); color: palette(highlighted-text); }
QPushButton#primary { font-weight: 600; }
"""
