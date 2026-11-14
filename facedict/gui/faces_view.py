"""A photo with clickable face boxes, a strip of face thumbnails, and the two combined."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from PySide6.QtCore import QEvent, QObject, QPointF, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (QButtonGroup, QFrame, QHBoxLayout, QScrollArea, QSizePolicy, QToolButton, QVBoxLayout,
                               QWidget)

from ..engine import display_crop
from ..imageio import resize_max_side
from ..models import Face
from .common import bgr_to_pixmap, muted

DISPLAY_MAX_SIDE = 1600
CHIP = 64

SELECTED = QColor(58, 190, 96)
HOVER = QColor(255, 196, 40)
OTHER = QColor(255, 255, 255)


@dataclass
class RenderedPhoto:
    """Produced off the GUI thread: a display-sized copy of the photo and a crop per face."""
    display: np.ndarray
    scale: float                       # display pixels per original pixel
    chips: list[np.ndarray]


def render_photo(image: np.ndarray, faces: list[Face]) -> RenderedPhoto:
    display, scale = resize_max_side(image, DISPLAY_MAX_SIDE)
    chips = [resize_max_side(display_crop(image, f), CHIP * 2)[0] for f in faces]
    return RenderedPhoto(np.ascontiguousarray(display), scale, chips)


class FaceImageView(QWidget):
    """Draws the photo scaled to fit and a box around each face. Click (or ←/→, 1–9) picks a face.
    With `double_click`, only a double-click picks one (for choices that are saved at once)."""

    face_clicked = Signal(int)

    def __init__(self, interactive: bool = True, min_size: QSize = QSize(260, 260), double_click: bool = False,
                 parent=None):
        super().__init__(parent)
        self.interactive = interactive
        self.double_click = double_click
        self.align_top = False                     # the photo starts at the top instead of the middle
        self._pixmap = QPixmap()
        self._scale = 1.0
        self._faces: list[Face] = []
        self._selected = -1
        self._hover = -1
        self.setMinimumSize(min_size)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMouseTracking(interactive)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus if interactive else Qt.FocusPolicy.NoFocus)

    # --- content -----------------------------------------------------------------------

    def set_content(self, pixmap: QPixmap, scale: float, faces: list[Face], selected: int = -1) -> None:
        self._pixmap, self._scale, self._faces = pixmap, scale, list(faces)
        self._selected = selected
        self._hover = -1
        self.update()

    def clear(self) -> None:
        self.set_content(QPixmap(), 1.0, [], -1)
        self.unsetCursor()

    def set_selected(self, index: int) -> None:
        self._selected = index
        self.update()

    @property
    def has_image(self) -> bool:
        return not self._pixmap.isNull()

    @property
    def face_count(self) -> int:
        return len(self._faces)

    @property
    def selected(self) -> int:
        return self._selected

    # --- geometry ------------------------------------------------------------------------

    def image_rect(self) -> QRectF:
        if self._pixmap.isNull():
            return QRectF()
        pw, ph = self._pixmap.width(), self._pixmap.height()
        k = min(self.width() / pw, self.height() / ph)
        w, h = pw * k, ph * k
        top = 0 if self.align_top else (self.height() - h) / 2
        return QRectF((self.width() - w) / 2, top, w, h)

    def face_rect(self, index: int) -> QRectF:
        r = self.image_rect()
        k = r.width() / self._pixmap.width() * self._scale
        x, y, w, h = self._faces[index].bbox
        return QRectF(r.x() + x * k, r.y() + y * k, w * k, h * k)

    def face_at(self, pos: QPointF) -> int:
        """The face under `pos`; boxes get a small margin so tiny faces stay clickable.
        With nested boxes, the smallest one wins."""
        best, best_area = -1, float("inf")
        for i in range(len(self._faces)):
            rect = self.face_rect(i)
            m = max(5.0, rect.width() * 0.12)
            if rect.adjusted(-m, -m, m, m).contains(pos):
                area = rect.width() * rect.height()
                if area < best_area:
                    best, best_area = i, area
        return best

    # --- painting -------------------------------------------------------------------------------

    def paintEvent(self, _event) -> None:
        if self._pixmap.isNull():
            return
        p = QPainter(self)
        p.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform)
        p.drawPixmap(self.image_rect(), self._pixmap, QRectF(self._pixmap.rect()))
        numbered = len(self._faces) > 1
        order = [i for i in range(len(self._faces)) if i not in (self._selected, self._hover)]
        order += [i for i in (self._hover, self._selected) if 0 <= i < len(self._faces)]
        for i in order:                                           # selected and hovered boxes on top
            rect = self.face_rect(i)
            color = SELECTED if i == self._selected else HOVER if i == self._hover else OTHER
            width = 3.0 if i in (self._selected, self._hover) else 1.6
            p.setPen(QPen(QColor(0, 0, 0, 110), width + 2))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRoundedRect(rect, 3, 3)
            p.setPen(QPen(color, width))
            p.drawRoundedRect(rect, 3, 3)
            if numbered:
                self._badge(p, rect, i, color)
        p.end()

    def _badge(self, p: QPainter, rect: QRectF, index: int, color: QColor) -> None:
        radius = 10.0
        cx = min(max(rect.left(), radius + 1), self.width() - radius - 1)
        cy = min(max(rect.top(), radius + 1), self.height() - radius - 1)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(color if index in (self._selected, self._hover) else QColor(20, 20, 20, 190))
        p.drawEllipse(QPointF(cx, cy), radius, radius)
        f = QFont(self.font())
        f.setPointSizeF(9.5)
        f.setBold(True)
        p.setFont(f)
        p.setPen(QColor(0, 0, 0) if index in (self._selected, self._hover) else QColor(255, 255, 255))
        p.drawText(QRectF(cx - radius, cy - radius, 2 * radius, 2 * radius), Qt.AlignmentFlag.AlignCenter,
                   str(index + 1))

    # --- interaction ------------------------------------------------------------------------------

    def mouseMoveEvent(self, event) -> None:
        if not self.interactive or not self._faces:
            return
        hover = self.face_at(event.position())
        if hover != self._hover:
            self._hover = hover
            if hover >= 0:
                self.setCursor(Qt.CursorShape.PointingHandCursor)
            else:
                self.unsetCursor()
            self.update()

    def leaveEvent(self, _event) -> None:
        if self._hover != -1:
            self._hover = -1
            self.update()

    def mousePressEvent(self, event) -> None:
        if not self.interactive or event.button() != Qt.MouseButton.LeftButton:
            return
        hit = self.face_at(event.position())
        if hit >= 0:
            self.setFocus()
            if not self.double_click:
                self.face_clicked.emit(hit)

    def mouseDoubleClickEvent(self, event) -> None:
        if not self.double_click:
            return self.mousePressEvent(event)
        if self.interactive and event.button() == Qt.MouseButton.LeftButton:
            hit = self.face_at(event.position())
            if hit >= 0:
                self.face_clicked.emit(hit)

    def keyPressEvent(self, event) -> None:
        n = len(self._faces)
        if not self.interactive or n < 2 or self.double_click:
            return super().keyPressEvent(event)
        key = event.key()
        cur = max(self._selected, 0)
        if key in (Qt.Key.Key_Right, Qt.Key.Key_Down):
            self.face_clicked.emit((cur + 1) % n)
        elif key in (Qt.Key.Key_Left, Qt.Key.Key_Up):
            self.face_clicked.emit((cur - 1) % n)
        elif Qt.Key.Key_1 <= key <= Qt.Key.Key_9 and key - Qt.Key.Key_1 < n:
            self.face_clicked.emit(key - Qt.Key.Key_1)
        else:
            super().keyPressEvent(event)


class _DoubleClickOnly(QObject):
    """Makes a button ignore single clicks and report double-clicks instead."""

    def __init__(self, strip: "FaceStrip"):
        super().__init__(strip)
        self.strip = strip

    def eventFilter(self, obj, event) -> bool:
        kind = event.type()
        if kind == QEvent.Type.MouseButtonDblClick and event.button() == Qt.MouseButton.LeftButton:
            if obj in self.strip.buttons:
                self.strip.face_clicked.emit(self.strip.buttons.index(obj))
            return True
        return kind in (QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonRelease)


class FaceStrip(QScrollArea):
    """A row of numbered face thumbnails; handy for small faces in group photos."""

    face_clicked = Signal(int)

    def __init__(self, double_click: bool = False, parent=None):
        super().__init__(parent)
        self.double_click = double_click
        self._double = _DoubleClickOnly(self) if double_click else None
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setFixedHeight(CHIP + 38)
        self._holder = QWidget()
        self._row = QHBoxLayout(self._holder)
        self._row.setContentsMargins(0, 0, 0, 0)
        self._row.setSpacing(4)
        self._row.addStretch(1)
        self.setWidget(self._holder)
        self.group = QButtonGroup(self)
        self.group.setExclusive(True)
        self.group.idClicked.connect(self._clicked)
        self.buttons: list[QToolButton] = []
        self._selected = -1

    def _clicked(self, index: int) -> None:
        if self.double_click:                       # only a double-click counts (see _DoubleClickOnly)
            self.set_selected(self._selected)
            return
        self.face_clicked.emit(index)

    def set_faces(self, chips: list[QPixmap], selected: int) -> None:
        self.clear()
        for i, chip in enumerate(chips):
            b = QToolButton()
            b.setCheckable(True)
            b.setIcon(QIcon(chip))
            b.setIconSize(QSize(CHIP, CHIP))
            b.setText(str(i + 1))
            b.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextUnderIcon)
            if self._double is not None:
                b.installEventFilter(self._double)
                b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            self.group.addButton(b, i)
            self._row.insertWidget(i, b)
            self.buttons.append(b)
        self.set_selected(selected)

    def set_selected(self, index: int) -> None:
        self._selected = index
        if 0 <= index < len(self.buttons):
            self.buttons[index].setChecked(True)
            self.ensureWidgetVisible(self.buttons[index])

    def clear(self) -> None:
        for b in self.buttons:
            self.group.removeButton(b)
            b.deleteLater()
        self.buttons = []


class QueryView(QWidget):
    """Photo with clickable faces + face strip + a one-line summary. Emits the chosen face."""

    face_selected = Signal(int)

    def __init__(self, interactive: bool = True, double_click: bool = False, parent=None):
        super().__init__(parent)
        self.interactive = interactive
        self.image = FaceImageView(interactive, double_click=double_click)
        self.strip = FaceStrip(double_click)
        self.info = muted()
        self._extra = ""
        self.image.face_clicked.connect(self._picked)
        self.strip.face_clicked.connect(self._picked)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.image, 1)
        lay.addWidget(self.strip)
        lay.addWidget(self.info)
        self.clear()

    def clear(self) -> None:
        self.image.clear()
        self.strip.clear()
        self.strip.setVisible(False)
        self.info.setText("")
        self._extra = ""

    def show_query(self, rendered: RenderedPhoto, faces: list[Face], selected: int, extra: str = "") -> None:
        self._extra = extra
        self.image.set_content(bgr_to_pixmap(rendered.display), rendered.scale, faces, selected)
        many = self.interactive and len(faces) > 1
        self.strip.setVisible(many)
        if many:
            self.strip.set_faces([bgr_to_pixmap(c) for c in rendered.chips], selected)
        self._update_info()

    def select(self, index: int) -> None:
        self.image.set_selected(index)
        self.strip.set_selected(index)
        self._update_info()

    def _picked(self, index: int) -> None:
        if index != self.image.selected:
            self.select(index)
            self.face_selected.emit(index)

    def _update_info(self) -> None:
        n, i = self.image.face_count, self.image.selected
        if n == 0:
            text = "No face found in this photo."
        elif n == 1:
            text = "1 face found."
        elif self.interactive:
            text = f"{n} faces found – face {i + 1} is selected."
        else:
            text = f"{n} faces found."
        self.info.setText(" ".join(t for t in (text, self._extra) if t))
