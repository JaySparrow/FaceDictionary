"""Library page: the list of entries, and one entry's page with its album.

There is no edit mode. Text reads like a label; double-click it to change it (a click is enough where
nothing is written yet), and click elsewhere to save. Photos are added, removed, reordered and switched
off right away. Every change can be undone with ⌘Z, step after step (see undo.py).
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QEvent, QItemSelectionModel, QPoint, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import (QAction, QColor, QFont, QFontMetrics, QGuiApplication, QIcon, QKeySequence, QPainter,
                           QPalette, QPen, QPixmap, QTextCursor, QUndoStack)
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QComboBox, QDialog, QDialogButtonBox, QFrame,
                               QHBoxLayout, QInputDialog, QLabel, QLineEdit, QListView, QListWidget,
                               QListWidgetItem, QMenu, QMessageBox, QPlainTextEdit, QProgressDialog,
                               QScrollArea, QSplitter, QToolTip, QVBoxLayout, QWidget)

from .. import config
from ..engine import iou, reading_order
from ..imageio import is_supported
from ..models import Entry, Face, OwnField, fold, split_values
from ..service import SCOPE_ALL, SCOPE_NAME, SCOPE_TAGS, haystacks
from . import undo
from .common import (GAP, GUTTER, PAGE_MARGIN, ElidedLabel, TwoLineDelegate, add_copy_action, add_menu_action,
                     bytes_to_pixmap, choose_folder, confirm, default_error_handler, divider, has_focus_in, header_row,
                     icon_button, make_selectable, mark_cover, mark_excluded, muted, open_images, placeholder_pixmap,
                     plural, rounded, select_rows, selected_items, semibold)
from .faces_view import QueryView, render_photo
from .history_page import fmt_time

ROLE_ID = Qt.ItemDataRole.UserRole
ROLE_SEARCH = Qt.ItemDataRole.UserRole + 1
ROLE_COVER = Qt.ItemDataRole.UserRole + 2
PHOTO_ICON = 168                  # the album's photos aim at this size and grow or shrink to fill the width
PHOTO_GAP = 12
PHOTO_RADIUS = 8
LABEL_WIDTH = 110
UNDO_LIMIT = 100


def dotted(text: str) -> str:
    return "  ·  ".join(split_values(text))


def repolish(widget: QWidget) -> None:
    widget.style().unpolish(widget)
    widget.style().polish(widget)
    widget.update()


# --- small widgets -------------------------------------------------------------------------------

def exec_menu(menu: QMenu, at: QPoint):
    return menu.exec(at)


def run_menu(menu_items: list, at: QPoint, parent: QWidget, marked=None) -> None:
    """Show a right-click menu of (title, callback) pairs (None: a separator). The chosen callback runs
    after the menu has closed, so it may safely rebuild the widgets the menu was opened on.
    `marked(on)` marks the place the menu belongs to while it is open."""
    menu = QMenu(parent)
    chosen = {}
    for item in menu_items:
        if item is None:
            menu.addSeparator()
        else:
            chosen[menu.addAction(item[0])] = item[1]
    if marked is not None:
        marked(True)
    try:
        action = exec_menu(menu, at)
    finally:
        if marked is not None:
            try:
                marked(False)
            except RuntimeError:                 # the row went away meanwhile (the app locked, …)
                pass
    if action in chosen:
        QTimer.singleShot(0, chosen[action])



class GrowingTextEdit(QPlainTextEdit):
    """A text box one line tall that grows with its content, up to `max_lines`, then scrolls."""

    def __init__(self, text: str = "", max_lines: int = 6, parent=None):
        super().__init__(text, parent)
        self.max_lines = max_lines
        self.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.textChanged.connect(self._sync_height)
        self._sync_height()

    def _sync_height(self) -> None:
        doc = self.document()
        doc.setTextWidth(max(50, self.viewport().width()))
        lines = max(1, min(self.max_lines, int(doc.size().height())))
        line = QFontMetrics(self.font()).lineSpacing()
        self.setFixedHeight(int(lines * line + 2 * doc.documentMargin() + 2 * self.frameWidth() + 2))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._sync_height()


class InlineText(QWidget):
    """Text that reads like a label and becomes a text box when double-clicked (a single click is
    enough while it is empty). Enter, Tab or a click anywhere else saves – ⌘Enter where the text can
    have several lines, since Enter starts a new line there; Esc puts the old text back."""

    committed = Signal(str)                # the new text, only when it changed
    started = Signal()
    finished = Signal()

    def __init__(self, text: str = "", *, multiline: bool = False, placeholder: str = "", hint: str = "",
                 display=None, label: QLabel | None = None, font: QFont | None = None, wrap: bool = True,
                 parent=None):
        super().__init__(parent)
        self.text = text
        self.multiline = multiline
        self.placeholder = placeholder     # shown, dimmed, while there is no text
        self.display = display             # text -> what the label shows (None: the text as it is)
        self.validate = None               # text -> a problem to point out, or None
        self.menu_actions: list = []              # more (title, callback) pairs for the menu, None: a separator
        self.on_menu = None                        # on_menu(True/False) while its right-click menu is open
        self.editing = False
        self.by_key = False                # the last edit ended with Enter or Tab (not a click)
        self.setFocusPolicy(Qt.FocusPolicy.ClickFocus)

        self.label = label if label is not None else QLabel()
        self.label.setProperty("inline", True)
        self.label.setTextFormat(Qt.TextFormat.PlainText)
        self.label.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)     # ours: contextMenuEvent
        self.label.setAlignment(Qt.AlignmentFlag.AlignLeft | (Qt.AlignmentFlag.AlignTop if multiline
                                                              else Qt.AlignmentFlag.AlignVCenter))
        if not isinstance(self.label, ElidedLabel):
            self.label.setWordWrap(wrap)
        make_selectable(self.label)                # a single click still selects text to copy
        self.editor = GrowingTextEdit() if multiline else QLineEdit()
        if multiline:
            self.editor.setTabChangesFocus(True)
        if hint:
            self.editor.setPlaceholderText(hint)
        if font is not None:
            self.label.setFont(font)
            self.editor.setFont(font)
        self.editor.hide()
        self.label.installEventFilter(self)
        self.editor.installEventFilter(self)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        lay.addWidget(self.label)
        lay.addWidget(self.editor)
        self._show()

    # --- what it shows --------------------------------------------------------------------------

    def set_text(self, text: str) -> None:
        self.text = text
        if not self.editing:
            self._show()

    @property
    def empty(self) -> bool:
        return not self.text.strip()

    def _show(self) -> None:
        shown = self.display(self.text) if self.display and not self.empty else self.text
        self.label.setText(self.placeholder if self.empty else shown)
        self._set_flag("empty", self.empty)

    def _set_flag(self, name: str, on: bool) -> None:
        if bool(self.label.property(name)) != on:
            self.label.setProperty(name, on)
            repolish(self.label)

    # --- editing ----------------------------------------------------------------------------------

    def start_edit(self, at: QPoint | None = None) -> None:
        """Open the text box; `at` (global) puts the cursor where the user double-clicked."""
        if self.editing:
            return
        self.editing = True
        self.by_key = False
        if self.multiline:
            self.editor.setPlainText(self.text)
        else:
            self.editor.setText(self.text)
        self.label.hide()
        self.editor.show()
        self.layout().activate()
        self.editor.setFocus(Qt.FocusReason.MouseFocusReason)
        self._place_cursor(at)
        QApplication.instance().installEventFilter(self)          # a click anywhere else saves
        self.started.emit()

    def _place_cursor(self, at: QPoint | None) -> None:
        if at is None or self.display is not None:               # the label shows other text: go to the end
            if self.multiline:
                self.editor.moveCursor(QTextCursor.MoveOperation.End)
            else:
                self.editor.end(False)
            return
        if self.multiline:
            self.editor.setTextCursor(self.editor.cursorForPosition(self.editor.viewport().mapFromGlobal(at)))
        else:
            self.editor.setCursorPosition(self.editor.cursorPositionAt(self.editor.mapFromGlobal(at)))

    def current(self) -> str:
        return (self.editor.toPlainText() if self.multiline else self.editor.text()).strip()

    def finish(self, save: bool = True, by_key: bool = False) -> bool:
        """Close the text box, saving (or not) what it says. False: the text has a problem and Enter
        was pressed, so the box stays open to fix it."""
        if not self.editing:
            return True
        new = self.current()
        changed = save and new != self.text.strip()
        if changed and self.validate is not None:
            problem = self.validate(new)
            if problem:
                where = self.editor.mapToGlobal(QPoint(0, self.editor.height()))
                if by_key:
                    QToolTip.showText(where, problem, self.editor)
                    return False
                QToolTip.showText(where, f"{problem} The old text was kept.", self)
                changed = False
        self.editing = False
        self.by_key = by_key
        QApplication.instance().removeEventFilter(self)
        if self.editor.hasFocus():
            self.setFocus(Qt.FocusReason.OtherFocusReason)        # not on to some other text box
        self.editor.hide()
        self.label.show()
        if changed:
            self.committed.emit(new)
        self.finished.emit()
        return True

    # --- events -------------------------------------------------------------------------------------

    def eventFilter(self, obj, event) -> bool:
        kind = event.type()
        if obj is self.label:
            if event.type() == QEvent.Type.MouseButtonDblClick and event.button() == Qt.MouseButton.LeftButton:
                self.start_edit(event.globalPosition().toPoint())
                return True
            if (kind == QEvent.Type.MouseButtonPress and event.button() == Qt.MouseButton.LeftButton
                    and self.empty):
                self.start_edit()
                return True
            return False
        if not self.editing:
            return False
        if obj is self.editor:
            if kind == QEvent.Type.KeyPress:
                key = event.key()
                if key == Qt.Key.Key_Escape:
                    self.finish(False)
                    return True
                if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                    command = event.modifiers() & (Qt.KeyboardModifier.ControlModifier
                                                   | Qt.KeyboardModifier.MetaModifier)
                    if not self.multiline or command:
                        self.finish(True, by_key=True)
                        return True
                if key in (Qt.Key.Key_Tab, Qt.Key.Key_Backtab):
                    self.by_key = True
            elif kind == QEvent.Type.FocusOut:
                if event.reason() not in (Qt.FocusReason.ActiveWindowFocusReason, Qt.FocusReason.PopupFocusReason):
                    self.finish(True, by_key=self.by_key)
            return False
        if kind == QEvent.Type.MouseButtonPress and isinstance(obj, QWidget):
            inside = obj is self.editor or self.editor.isAncestorOf(obj)
            if not inside and not isinstance(obj.window(), QMenu):
                self.finish(True)
        return False

    def contextMenuEvent(self, event) -> None:
        if self.editing:
            return super().contextMenuEvent(event)
        items = []
        if not self.empty:
            selected = self.label.selectedText() if self.label.hasSelectedText() else ""
            text = selected or self.text
            items.append(("Copy", lambda: QGuiApplication.clipboard().setText(text)))
        items.append(("Add…" if self.empty else "Edit…", lambda: self.start_edit()))
        items += self.menu_actions
        run_menu(items, event.globalPos(), self, self.on_menu)


class FieldRow(QWidget):
    """One field of an entry: its name on the left, its text on the right, both edited in place.
    While its right-click menu is open, the row is marked, so it is clear which field the menu is for."""

    def __init__(self, name: str, value: str, personal: bool, parent=None):
        super().__init__(parent)
        self.personal = personal
        self.created = False                       # a new row: set once the field exists
        self.field_name = name
        self.marked = False
        self.name = InlineText(name, placeholder="Field name", hint="Field name")
        self.name.setFixedWidth(LABEL_WIDTH)
        value_font = QFont(self.font())
        value_font.setPointSize(14)
        self.value = InlineText(value, multiline=True, placeholder="—", font=value_font)
        self.name.on_menu = self.value.on_menu = self.set_marked
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)
        lay.addWidget(self.name, 0, Qt.AlignmentFlag.AlignTop)
        lay.addWidget(self.value, 1)

    def set_value(self, value: str) -> None:
        self.value.set_text(value)

    def set_marked(self, on: bool) -> None:
        self.marked = on
        self.update()

    def paintEvent(self, event) -> None:
        if self.marked:
            color = self.palette().color(QPalette.ColorRole.Accent)
            color.setAlpha(45)
            p = QPainter(self)
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(color)
            p.drawRoundedRect(QRectF(self.rect()).adjusted(-3, -1, 3, 1), 5, 5)
            p.end()
        super().paintEvent(event)


def dropped_images(mime) -> list:
    """Photo files (Path) or image data (bytes) carried by a drag from outside the app."""
    files = [Path(u.toLocalFile()) for u in mime.urls() if u.isLocalFile() and is_supported(u.toLocalFile())]
    if files:
        return files
    if mime.hasImage():
        from PySide6.QtGui import QImage
        from .common import qimage_to_png_bytes
        image = QImage(mime.imageData())
        if not image.isNull():
            return [qimage_to_png_bytes(image)]
    return []


class FitScrollArea(QScrollArea):
    """As tall as what it holds, up to half the window, then it scrolls. It follows its content: rows
    added or removed, text that wraps differently at another width, a text box that grows."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.fit)

    def setWidget(self, widget: QWidget) -> None:
        super().setWidget(widget)
        widget.installEventFilter(self)

    def eventFilter(self, obj, event) -> bool:
        if event.type() in (QEvent.Type.LayoutRequest, QEvent.Type.Resize):
            self.fit_later()
        return False

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if event.oldSize().width() != event.size().width():
            self.fit_later()

    def fit_later(self) -> None:
        self._timer.start(0)

    def fit(self) -> None:
        widget = self.widget()
        if widget is None or widget.layout() is None:
            return
        layout = widget.layout()
        width = max(200, self.viewport().width())
        needed = layout.totalHeightForWidth(width) if layout.hasHeightForWidth() else layout.totalSizeHint().height()
        height = min(needed + 2 * self.frameWidth(), max(120, self.window().height() // 2))
        if self.maximumHeight() != height or self.minimumHeight() != height:
            self.setFixedHeight(height)


class PhotoGrid(QListWidget):
    """The album grid. Photos always snap to the grid and pack to the left; dragging one either
    inserts it between two photos or swaps it with the photo it is dropped on (several selected photos
    move together and are only inserted). The first photo, the cover, has a frame. Photos dragged in
    from outside are added."""

    reordered = Signal(list)                       # photo ids in the new order
    dropped = Signal(list)                         # photos dragged in from outside: Path or bytes

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setViewMode(QListView.ViewMode.IconMode)
        self.setMovement(QListView.Movement.Static)          # items snap back into the grid
        self.setResizeMode(QListView.ResizeMode.Adjust)
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setDropIndicatorShown(False)                    # we draw our own
        self._drag_rows: list[int] = []                      # the photos being dragged
        self._drop: tuple[str, int] | None = None
        self._outside = False                      # the drag comes from outside the app
        self.setDragEnabled(True)
        self.viewport().setAcceptDrops(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._fit()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._fit()

    def sizeHint(self) -> QSize:
        return QSize(400, 200)                          # small: the album takes whatever room is left

    def _fit(self) -> None:
        """As many columns as photos of about PHOTO_ICON fit, then make them exactly fill the width."""
        width = max(1, self.viewport().width() - 2)
        columns = max(1, round((width + PHOTO_GAP) / (PHOTO_ICON + PHOTO_GAP)))
        side = max(96, min(260, (width - columns * PHOTO_GAP) // columns))
        if self.iconSize().width() != side:
            self.setIconSize(QSize(side, side))
            self.setGridSize(QSize(side + PHOTO_GAP, side + PHOTO_GAP))

    def ids(self) -> list[str]:
        return [self.item(i).data(ROLE_ID) for i in range(self.count())]

    def _rect(self, row: int):
        return self.visualRect(self.indexFromItem(self.item(row)))

    # --- what a drop at this point would do -------------------------------------------------

    def target_at(self, pos, swap: bool = True) -> tuple[str, int]:
        index = self.indexAt(pos)
        if index.isValid():
            rect = self.visualRect(index)
            frac = (pos.x() - rect.left()) / max(1, rect.width())
            if swap and 0.3 <= frac <= 0.7:
                return "swap", index.row()
            return "insert", index.row() + (0 if frac < (0.3 if swap else 0.5) else 1)
        for i in range(self.count()):
            rect = self._rect(i)
            if pos.y() < rect.top():
                return "insert", i
            if rect.top() <= pos.y() <= rect.bottom() and pos.x() < rect.center().x():
                return "insert", i
        return "insert", self.count()

    @staticmethod
    def new_order(ids: list[str], sources: list[int], kind: str, target: int) -> list[str]:
        """The order after dropping the photos at rows `sources`: they swap with the photo at `target`
        (one photo only) or go in, as a block in their own order, before the photo at `target`."""
        ids, rows = list(ids), sorted(sources)
        if kind == "swap" and len(rows) == 1:
            ids[rows[0]], ids[target] = ids[target], ids[rows[0]]
            return ids
        rest = [pid for i, pid in enumerate(ids) if i not in rows]
        at = target - sum(r < target for r in rows)
        return rest[:at] + [ids[r] for r in rows] + rest[at:]

    # --- drag and drop ------------------------------------------------------------------------

    def startDrag(self, actions):
        self._drag_rows = sorted(i.row() for i in self.selectedIndexes()) or [self.currentRow()]
        try:
            super().startDrag(actions)
        finally:
            self._drag_rows = []
            self._drop = None
            self.viewport().update()

    def dragEnterEvent(self, event):
        self._outside = not self._drag_rows and bool(dropped_images(event.mimeData()))
        if self._drag_rows:
            event.acceptProposedAction()
        elif self._outside:
            event.setDropAction(Qt.DropAction.CopyAction)
            event.accept()
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        if self._outside:
            event.setDropAction(Qt.DropAction.CopyAction)
            return event.accept()
        if not self._drag_rows:
            return event.ignore()
        self._drop = self.target_at(event.position().toPoint(), swap=len(self._drag_rows) == 1)
        event.setDropAction(Qt.DropAction.MoveAction)
        event.accept()
        self.viewport().update()

    def dragLeaveEvent(self, event):
        self._drop = None
        self.viewport().update()
        super().dragLeaveEvent(event)

    def dropEvent(self, event):
        if self._outside:
            self._outside = False
            items = dropped_images(event.mimeData())
            event.setDropAction(Qt.DropAction.CopyAction)
            event.accept()
            if items:
                QTimer.singleShot(0, lambda: self.dropped.emit(items))
            return
        drop, sources = self._drop, self._drag_rows
        self._drop = None
        self.viewport().update()
        if drop is None or not sources:
            return event.ignore()
        order = self.new_order(self.ids(), sources, *drop)
        event.setDropAction(Qt.DropAction.IgnoreAction)      # the page rebuilds the grid itself
        event.accept()
        if order != self.ids():
            QTimer.singleShot(0, lambda: self.reordered.emit(order))

    # --- the blue hint that tells insert from swap -----------------------------------------------

    def paintEvent(self, event):
        super().paintEvent(event)
        if self._drop is None or not self.count():
            return
        painter = QPainter(self.viewport())
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        kind, target = self._drop
        color = self.palette().color(QPalette.ColorRole.Accent)
        if kind == "swap":
            rect = self._rect(target).adjusted(2, 2, -2, -2)
            painter.setBrush(QColor(color.red(), color.green(), color.blue(), 70))
            painter.setPen(QPen(color, 2))
            painter.drawRoundedRect(rect, 8, 8)
        else:
            if target < self.count():
                rect = self._rect(target)
                x = rect.left() - 5
            else:
                rect = self._rect(self.count() - 1)
                x = rect.right() + 5
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(color.red(), color.green(), color.blue(), 210))
            painter.drawRoundedRect(QRectF(x - 2.5, rect.top() + 2, 5, rect.height() - 4), 2.5, 2.5)
        painter.end()


class PhotoDialog(QDialog):
    """A photo of an album, with its faces, and a note of the user's own under it. Double-clicking a
    face makes it the one used for matching; a single click changes nothing, so a stray click cannot."""

    face_changed = Signal(str, object, object)            # photo id, the face used before, the one used now

    def __init__(self, service, runner, entry_id: str, photo_ref, image, faces: list[Face], parent=None):
        super().__init__(parent)
        self.service = service
        self.runner = runner
        self.entry_id = entry_id
        self.ref = photo_ref
        self.faces = [faces[i] for i in reading_order(faces)]
        self.setWindowTitle("Photo")
        screen = (parent.screen() if parent is not None else QApplication.primaryScreen()).availableGeometry()
        self.resize(int(screen.width() * 0.72), int(screen.height() * 0.85))

        current = -1
        if photo_ref.face is not None and self.faces:
            overlaps = [iou(photo_ref.face, f) for f in self.faces]
            current = max(range(len(self.faces)), key=overlaps.__getitem__) if max(overlaps) > 0.5 else -1
        interactive = bool(self.faces) and (len(self.faces) > 1 or current < 0)
        self.view = QueryView(interactive=interactive, double_click=True)
        self.view.info.hide()
        self.view.show_query(render_photo(image, self.faces), self.faces, current)
        if interactive:
            self.view.face_selected.connect(self._apply)

        self.save_note = None             # save_note(photo id, old, new) -> bool; set by the page (undo)
        self.note = InlineText(photo_ref.note, multiline=True, placeholder="Note")
        self.note.committed.connect(self._note_committed)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        lay = QVBoxLayout(self)
        lay.addWidget(self.view, 1)
        lay.addWidget(self.note)
        lay.addWidget(buttons)

    def _note_committed(self, text: str) -> None:
        old = self.ref.note
        if self.save_note is not None:
            ok = self.save_note(self.ref.photo_id, old, text)
        else:
            self.service.set_photo_note(self.entry_id, self.ref.photo_id, text)
            ok = True
        if ok:
            self.ref.note = text
            self.note.set_text(text)

    def done(self, result: int) -> None:
        self.note.finish(True)                # closing (or locking) keeps what was typed
        super().done(result)

    def _apply(self, index: int) -> None:
        photo_id, before = self.ref.photo_id, self.ref.face

        def done(ref):
            self.ref = ref
            self.face_changed.emit(photo_id, before, ref.face)

        def failed(exc):
            QMessageBox.warning(self, "Photo", str(exc))

        self.runner.submit(lambda: self.service.choose_face(self.entry_id, photo_id, self.faces[index]),
                           on_done=done, on_error=failed)


# --- page ----------------------------------------------------------------------------------------

class LibraryPage(QWidget):
    changed = Signal()
    reveal = Signal()                      # an undo or redo step: bring the Library page to the front

    def __init__(self, service, runner, parent=None):
        super().__init__(parent)
        self.service = service
        self.runner = runner
        self.entry: Entry | None = None
        self.undo_stack = QUndoStack(self)
        self.undo_stack.setUndoLimit(UNDO_LIMIT)
        self._active: InlineText | None = None                 # the text box open right now
        self._rows: dict[tuple, FieldRow] = {}                  # ("field", id) / ("own", name) -> row
        self._divider: QWidget | None = None                    # between shared and own fields
        self.add_actions = {                                     # in the order of the page
            "shared": self._action("Add Field to All Entries", "Ctrl+Shift+;",
                                   lambda: self._add_field_action("shared")),
            "own": self._action("Add Field to This Entry", "Ctrl+;", lambda: self._add_field_action("own")),
        }
        self._body_key = None                                    # what the field rows were built from
        self._light = False
        self._push_failed = False

        split = QSplitter()
        split.setHandleWidth(GUTTER)
        split.addWidget(self._build_list())
        split.addWidget(self._build_entry())
        split.setStretchFactor(0, 2)
        split.setStretchFactor(1, 5)
        split.setChildrenCollapsible(False)
        split.setSizes([300, 950])
        lay = QVBoxLayout(self)
        lay.setContentsMargins(PAGE_MARGIN, PAGE_MARGIN, PAGE_MARGIN, PAGE_MARGIN)
        lay.addWidget(split)
        self._show_entry(False)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.body_scroll.fit_later()                        # its limit is half the window

    def _action(self, text: str, shortcut: str, slot) -> QAction:
        action = QAction(text, self)
        action.setShortcut(QKeySequence(shortcut))
        action.triggered.connect(slot)
        return action

    def delete_target(self, focus):
        """What ⌫ deletes: the selected photos in the album, or the selected entries in the list.
        Fields have no selection, so they are not deleted by key (clear the name instead)."""
        if has_focus_in(self.photos, focus) and self._selected_photos():
            return self.remove_photo
        if has_focus_in(self.list, focus) and self._targets():
            return self.delete_entry
        return None

    def _add_field_action(self, kind: str) -> None:
        if not self.service.unlocked or self.entry is None:
            QApplication.beep()
            return
        self.reveal.emit()
        self.new_field(kind)

    # --- left: the list of entries ------------------------------------------------------------

    def _build_list(self) -> QWidget:
        self.scope = QComboBox()
        self.scope.setToolTip("Where to search")
        self.scope.currentIndexChanged.connect(self._filter_changed)
        self.filter = QLineEdit()
        self.filter.setPlaceholderText("Search the library")
        self.filter.setClearButtonEnabled(True)
        self.filter.textChanged.connect(self._filter_changed)
        search_row = QHBoxLayout()
        search_row.setSpacing(4)
        search_row.addWidget(self.scope)
        search_row.addWidget(self.filter, 1)

        self.list = QListWidget()
        self.list.setIconSize(QSize(44, 44))
        self.list.setItemDelegate(TwoLineDelegate(44, round_picture=True, parent=self.list))
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.list.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.list.itemSelectionChanged.connect(self._selection_changed)
        add_copy_action(self.list)
        self.export_action = add_menu_action(self.list, self.export_entry, separator=True)
        self.trash_action = add_menu_action(self.list, self.delete_entry)
        self.count = muted()
        new_btn = icon_button("plus", "New entry (⌘N)")
        new_btn.clicked.connect(self.new_entry)
        import_btn = icon_button("import", "Import a folder…")
        import_btn.clicked.connect(self.import_folder)

        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        ll.setSpacing(GAP)
        ll.addWidget(header_row("Library", new_btn, import_btn))
        ll.addLayout(search_row)
        ll.addWidget(self.list, 1)
        ll.addWidget(self.count)
        return left

    # --- right: one entry --------------------------------------------------------------------------

    def _build_entry(self) -> QWidget:
        title_font = semibold(self.font(), 28)
        self.title = self._track(InlineText(font=title_font, wrap=False))
        self.title.validate = lambda text: None if text.strip() else "The entry needs a name."
        self.title.committed.connect(self._name_committed)
        aliases = ElidedLabel()
        aliases.setObjectName("subtitle")
        self.aliases_text = self._track(InlineText(placeholder="Aliases", label=aliases,
                                                   display=lambda t: "·  " + dotted(t)))
        self.aliases_text.committed.connect(lambda text: self._list_committed("aliases", "Aliases", text))
        tags = QLabel()
        tags.setObjectName("tags")
        self.tags_text = self._track(InlineText(placeholder="Tags", label=tags, display=dotted))
        self.tags_text.committed.connect(lambda text: self._list_committed("tags", "Tags", text))

        self.export_btn = icon_button("export", "Export an unencrypted copy…")
        self.export_btn.clicked.connect(self.export_entry)
        self.delete_btn = icon_button("trash", "Move this entry and its photos to the Trash")
        self.delete_btn.clicked.connect(self.delete_entry)
        header = header_row("", self.export_btn, self.delete_btn)
        header.layout().insertWidget(0, self.title)
        header.layout().insertWidget(1, self.aliases_text, 1)

        self.body = QVBoxLayout()
        self.body.setContentsMargins(0, 0, 0, 0)                # fields line up with the name above
        self.body.setSpacing(GAP)
        self.body.addStretch(1)
        self.body_holder = QWidget()
        self.body_holder.setLayout(self.body)
        self.body_holder.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.body_holder.customContextMenuRequested.connect(self._body_menu)
        self.body_scroll = FitScrollArea()
        self.body_scroll.setWidgetResizable(True)
        self.body_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.body_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.body_scroll.setWidget(self.body_holder)

        self.photos_title = QLabel("Photos")
        self.photos_title.setFont(semibold(self.photos_title.font(), 15))
        make_selectable(self.photos_title)
        self.add_photo_btn = icon_button("plus", "Add photos to this entry (⌘O)")
        self.add_photo_btn.clicked.connect(self.add_photos)
        self.remove_photo_btn = icon_button("minus", "Move the selected photo to the Trash")
        self.remove_photo_btn.clicked.connect(self.remove_photo)
        self.exclude_btn = icon_button("ban", "Do not use the selected photo for matching", checkable=True)
        self.exclude_btn.clicked.connect(self.toggle_excluded)
        photos_header = QHBoxLayout()
        photos_header.addWidget(self.photos_title)
        photos_header.addStretch(1)
        for b in (self.add_photo_btn, self.remove_photo_btn, self.exclude_btn):
            photos_header.addWidget(b)

        self.photos = PhotoGrid()
        self.photos.setMinimumHeight(140)
        self.photos.itemDoubleClicked.connect(lambda _i: self.open_photo())
        self.photos.itemSelectionChanged.connect(self._update_photo_buttons)
        self.photos.reordered.connect(self._photos_reordered)
        self.photos.dropped.connect(lambda items: self.add_photo_files(self.entry.id, items) if self.entry else None)

        self.stamps = muted()

        self.entry_widget = QWidget()
        el = self.entry_layout = QVBoxLayout(self.entry_widget)
        el.setContentsMargins(0, 0, 0, 0)
        el.setSpacing(GAP)
        el.addWidget(header)
        el.addWidget(self.tags_text)
        el.addWidget(self.body_scroll)                  # as tall as its fields; the album takes the rest
        el.addLayout(photos_header)
        el.addWidget(self.photos, 1)
        el.addWidget(self.stamps)

        self.placeholder = muted()
        self.placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.addWidget(self.placeholder)
        rl.addWidget(self.entry_widget, 1)
        return right

    # --- the text box that is open ----------------------------------------------------------------

    def _track(self, inline: InlineText) -> InlineText:
        inline.started.connect(lambda: setattr(self, "_active", inline))
        inline.finished.connect(lambda: setattr(self, "_active", None) if self._active is inline else None)
        return inline

    def _editing(self) -> bool:
        try:
            return self._active is not None and self._active.editing
        except RuntimeError:                       # its widget is gone
            self._active = None
            return False

    def commit_edits(self) -> None:
        """Save the text box that is open (before locking, switching entries, undoing …)."""
        if self._editing():
            self._active.finish(True)

    @property
    def has_unsaved_changes(self) -> bool:
        return self._editing()

    # --- list handling -------------------------------------------------------------------------------

    def refresh(self, select: str | list[str] | None = None) -> None:
        """Show the library as it is now, with these entries selected (None: the ones selected now)."""
        if not self.service.unlocked:
            return
        if select:
            keep = [select] if isinstance(select, str) else list(select)
        else:
            keep = [i.data(ROLE_ID) for i in self.list.selectedItems()] or ([self.entry.id] if self.entry else [])
        entries = self.service.list_entries()
        field_ids = [f.id for f in self.service.global_fields()]
        self._fill_scopes()
        self.list.blockSignals(True)
        self.list.clear()
        for e in entries:
            item = QListWidgetItem(self._list_text(e))
            pm = placeholder_pixmap(40, e.name[:1].upper() or "?")
            if e.cover is not None:
                try:
                    pm = bytes_to_pixmap(self.service.thumb_bytes(e.cover.photo_id, e.cover.face))
                except Exception:
                    pass
            item.setIcon(QIcon(pm))
            item.setData(ROLE_ID, e.id)
            item.setData(ROLE_SEARCH, haystacks(e, field_ids))
            self.list.addItem(item)
        rows = [i for i, e in enumerate(entries) if e.id in keep]
        select_rows(self.list, rows, next((r for r in rows if entries[r].id == keep[0]), rows[0]) if rows else None)
        self.list.blockSignals(False)
        self.count.setText(f"{len(entries)} entries · {sum(len(e.photos) for e in entries)} photos")
        self._apply_filter()
        shown = self._selected_ids()
        if len(shown) > 1:
            self._show_several(len(shown))
        else:                                         # one entry stays shown while the filter hides it
            self._load_entry(shown[0] if shown else keep[0] if len(keep) == 1 else None)

    @staticmethod
    def _list_text(e: Entry) -> str:
        return f"{e.name}\n{len(e.photos)} photo(s)"

    def _update_list_item(self, e: Entry) -> None:
        field_ids = [f.id for f in self.service.global_fields()]
        for i in range(self.list.count()):
            item = self.list.item(i)
            if item.data(ROLE_ID) == e.id:
                item.setText(self._list_text(e))
                item.setData(ROLE_SEARCH, haystacks(e, field_ids))
        self._apply_filter()

    def _fill_scopes(self) -> None:
        wanted = [("All fields", SCOPE_ALL), ("Name", SCOPE_NAME), ("Tags", SCOPE_TAGS)]
        wanted += [(f.name, f.id) for f in self.service.global_fields()]
        if [(self.scope.itemText(i), self.scope.itemData(i)) for i in range(self.scope.count())] == wanted:
            return
        keep = self.scope.currentData()
        self.scope.blockSignals(True)
        self.scope.clear()
        for label, data in wanted:
            self.scope.addItem(label, data)
        index = self.scope.findData(keep)
        self.scope.setCurrentIndex(index if index >= 0 else 0)
        self.scope.blockSignals(False)

    def _filter_changed(self) -> None:
        self._apply_filter()
        if len(self.list.selectedItems()) > 1:
            self._selection_changed()               # only the entries the filter shows count as selected

    def _apply_filter(self) -> None:
        needle = fold(self.filter.text())
        scope = self.scope.currentData() or SCOPE_ALL
        for i in range(self.list.count()):
            item = self.list.item(i)
            hay = (item.data(ROLE_SEARCH) or {}).get(scope, "")
            item.setHidden(bool(needle) and needle not in hay)

    def select_entry(self, entry_id: str) -> None:
        self.filter.clear()
        for i in range(self.list.count()):
            if self.list.item(i).data(ROLE_ID) == entry_id:
                self.list.setCurrentRow(i, QItemSelectionModel.SelectionFlag.ClearAndSelect)
                return

    def _selected_ids(self) -> list[str]:
        return [i.data(ROLE_ID) for i in selected_items(self.list)]

    def _targets(self) -> list[str]:
        """The entries a delete or an export is for: those selected, or the one shown."""
        return self._selected_ids() or ([self.entry.id] if self.entry else [])

    def _selection_changed(self) -> None:
        ids = self._selected_ids()
        if len(ids) > 1:
            self._show_several(len(ids))
        else:
            self._load_entry(ids[0] if ids else None)

    def _show_several(self, n: int) -> None:
        self._load_entry(None)
        self.placeholder.setText(f"{n} entries selected")

    def _update_menu(self) -> None:
        n = len(self._targets())
        what = plural(n, "Entry", "Entries")
        self.export_action.setText(f"Export {what}…")
        self.trash_action.setText(f"Move {what} to Trash")
        for action in (self.export_action, self.trash_action):
            action.setEnabled(n > 0)

    # --- showing one entry ------------------------------------------------------------------------------

    def _show_entry(self, on: bool) -> None:
        self.entry_widget.setVisible(on)
        self.placeholder.setVisible(not on)

    def _load_entry(self, entry_id: str | None) -> None:
        if self._editing() and (self.entry is None or entry_id != self.entry.id):
            self.commit_edits()                                  # never lose what is being typed
        ok = entry_id and self.service.unlocked and self.service.has_entry(entry_id)
        self.entry = self.service.get_entry(entry_id) if ok else None
        self._show_entry(self.entry is not None)
        self.placeholder.setText("")
        self._update_menu()
        if self.entry is None:
            self.photos.clear()
            self._body_key = None
            return
        self._update_header()
        if not self._editing() and self._fields_key() != self._body_key:
            self._build_body()                    # rebuilt only when the fields changed (keeps the scroll)
        self._load_photos()

    def _update_header(self) -> None:
        e = self.entry
        self.title.set_text(e.name)
        self.aliases_text.set_text(", ".join(e.aliases))
        self.tags_text.set_text(", ".join(e.tags))
        self.stamps.setText(f"Created {fmt_time(e.created_at)}  ·  updated {fmt_time(e.updated_at)}")

    def _fields_key(self):
        e = self.entry
        return (e.id, tuple(sorted(e.fields.items())), tuple((f.name, f.value) for f in e.own),
                tuple((f.id, f.name) for f in self.service.global_fields()))

    def _build_body(self) -> None:
        e = self.entry
        same = self._body_key is not None and self._body_key[0] == e.id
        scroll = self.body_scroll.verticalScrollBar().value() if same else 0
        while self.body.count() > 1:                             # keep the trailing stretch
            item = self.body.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._rows, self._divider = {}, None
        for spec in self.service.global_fields():
            self._add_row(("field", spec.id), spec.name, e.fields.get(spec.id, ""), personal=False)
        if e.own:
            self._add_divider()
        for own in e.own:
            self._add_row(("own", own.name), own.name, own.value, personal=True)
        self._body_key = self._fields_key()
        self.body_scroll.fit_later()                        # its height follows the fields
        if scroll:
            QTimer.singleShot(0, lambda: self.body_scroll.verticalScrollBar().setValue(scroll))

    def _add_divider(self) -> None:
        self._divider = divider()
        self._insert(self._divider)

    def _add_menu(self) -> list:
        return [(a.text(), a.trigger) for a in self.add_actions.values()]

    def _body_menu(self, pos) -> None:
        """Right-click beside the fields."""
        if self.entry is not None:
            run_menu(self._add_menu(), self.body_holder.mapToGlobal(pos), self)

    def _insert(self, widget: QWidget, before: QWidget | None = None) -> None:
        index = self.body.indexOf(before) if before is not None else self.body.count() - 1
        self.body.insertWidget(index, widget)

    def _add_row(self, key: tuple, name: str, value: str, personal: bool) -> FieldRow:
        row = FieldRow(name, value, personal)
        self._track(row.name)
        self._track(row.value)
        row.value.committed.connect(lambda text, key=key, name=name: self._value_committed(key, name, text))
        row.name.validate = lambda text, key=key: (self._field_name_problem(text, exclude=key) if text.strip()
                                                   else None)      # no name: the field is removed
        row.name.committed.connect(lambda text, key=key, name=name: self._name_committed_for(key, name, text))
        menu = [(f"Remove “{name}”", lambda key=key, name=name: self.remove_field(key, name)), None]
        row.name.menu_actions = row.value.menu_actions = menu + self._add_menu()
        self._insert(row)
        self._rows[key] = row
        return row

    # --- undo history --------------------------------------------------------------------------------

    def _callbacks(self) -> dict:
        return {"done": self._step_done, "failed": self._step_failed}

    def _push(self, command, light: bool = False) -> bool:
        """Make a change through the undo history. `light`: the page already shows it (it was typed),
        so only the header and the list are brought up to date."""
        self._light, self._push_failed = light, False
        try:
            self.undo_stack.push(command)
        finally:
            self._light = False
        return not self._push_failed

    def _step_done(self, entry_id: str | list[str] | None) -> None:
        if self._light and isinstance(entry_id, str) and self.entry is not None and entry_id == self.entry.id:
            self.entry = self.service.get_entry(entry_id)
            self._update_header()
            self._update_list_item(self.entry)
            self._body_key = self._fields_key()
        else:
            self.refresh(entry_id)
        self.changed.emit()

    def _step_failed(self, command, exc: Exception) -> None:
        self._push_failed = True
        what = {"do": "save the change", "undo": f"undo “{command.text()}”",
                "redo": f"redo “{command.text()}”"}[command.phase]
        QMessageBox.warning(self, "FaceDictionary", f"Could not {what}: {exc}")
        if command.phase != "do":
            self.refresh()
            self.changed.emit()

    def undo(self) -> None:
        if self._editing():                       # Edit › Undo while typing: the text box's own undo
            self._active.editor.undo()
            return
        self._step(self.undo_stack.undo, self.undo_stack.canUndo())

    def redo(self) -> None:
        if self._editing():
            self._active.editor.redo()
            return
        self._step(self.undo_stack.redo, self.undo_stack.canRedo())

    def _step(self, step, possible: bool) -> None:
        if not possible or not self.service.unlocked:
            return
        if self.runner.busy or QApplication.activeModalWidget() is not None:
            QApplication.beep()                   # wait until photos are added, a dialog is closed …
            return
        self.reveal.emit()
        step()

    # --- text -------------------------------------------------------------------------------------------

    def _name_committed(self, text: str) -> None:
        if self.entry is None:
            return
        self._push(undo.SetText(self.service, self.entry.id, "name", self.entry.name, text.strip(), "Name",
                                **self._callbacks()), light=True)

    def _list_committed(self, key: str, label: str, text: str) -> None:
        if self.entry is None:
            return
        old, new = list(getattr(self.entry, key)), split_values(text)
        if new == old:
            self._update_header()                 # only the commas changed: show it tidied up
            return
        self._push(undo.SetText(self.service, self.entry.id, key, old, new, label, **self._callbacks()),
                   light=True)

    def _value_committed(self, key: tuple, name: str, text: str) -> None:
        e = self.entry
        if e is None:
            return
        if key[0] == "field":
            old = e.fields.get(key[1], "")
        else:
            own = e.own_field(key[1])
            old = own.value if own else ""
        if self._push(undo.SetText(self.service, e.id, key, old, text, f"“{name}”", **self._callbacks()),
                      light=True):
            row = self._rows.get(key)
            if row is not None:
                row.set_value(text)

    # --- fields ----------------------------------------------------------------------------------------

    def _field_name_problem(self, name: str, exclude: tuple | None = None) -> str | None:
        name = name.strip()
        if not name:
            return "A field needs a name."
        taken = [(("field", f.id), f.name) for f in self.service.global_fields()]
        taken += [(("own", f.name), f.name) for f in self.entry.own]
        if any(fold(n) == fold(name) and key != exclude for key, n in taken):
            return f"There is already a field called “{name}”."
        return None

    def new_field(self, kind: str) -> None:
        """Add Field to All Entries / to This Entry: a new row at the end of that part, whose name is
        typed in place. Enter goes on to its text; Esc, or leaving the name empty, adds nothing."""
        if self.entry is None:
            return
        self.commit_edits()
        row = FieldRow("", "", kind == "own")
        if kind == "shared":
            self._insert(row, before=self._divider)
        else:
            if self._divider is None:
                self._add_divider()
            self._insert(row)
        self._track(row.name)
        row.name.validate = self._field_name_problem
        row.name.committed.connect(lambda name: self._create_field(kind, name.strip(), row))
        row.name.finished.connect(lambda: self._new_field_closed(row))
        row.name.start_edit()

    def _create_field(self, kind: str, name: str, row: FieldRow) -> None:
        e = self.entry
        row.created = True
        if kind == "shared":
            command = undo.SharedFieldChange.new(self.service, e.id, name, **self._callbacks())
            key = ("field", command.field_id)
        else:
            command = undo.OwnFieldChange(self.service, e.id, OwnField(name, ""), len(e.own), True,
                                          **self._callbacks())
            key = ("own", name)
        if self._push(command) and row.name.by_key:
            QTimer.singleShot(0, lambda: self.edit_field(key))     # Enter: go on to the field's text

    def _new_field_closed(self, row: FieldRow) -> None:
        if row.created:
            return
        self.body.removeWidget(row)
        row.deleteLater()
        if self._divider is not None and self.entry is not None and not self.entry.own:
            self.body.removeWidget(self._divider)              # it only separates own fields
            self._divider.deleteLater()
            self._divider = None

    def edit_field(self, key: tuple) -> None:
        row = self._rows.get(key)
        if row is not None:
            row.value.start_edit()

    def _name_committed_for(self, key: tuple, name: str, text: str) -> None:
        if text.strip():
            self._rename_field(key, text)
        else:
            self.remove_field(key, name)

    def _rename_field(self, key: tuple, new: str) -> None:
        e = self.entry
        if key[0] == "field":
            spec = next((f for f in self.service.global_fields() if f.id == key[1]), None)
            if spec is None:
                return
            command = undo.RenameField(self.service, e.id, spec.id, spec.name, new.strip(), **self._callbacks())
        else:
            command = undo.RenameField(self.service, e.id, None, key[1], new.strip(), **self._callbacks())
        self._push(command)

    def remove_field(self, key: tuple, name: str) -> None:
        e = self.entry
        if e is None:
            return
        self.commit_edits()
        if key[0] == "own":
            index = next((i for i, f in enumerate(e.own) if f.name == key[1]), None)
            if index is not None:
                self._push(undo.OwnFieldChange(self.service, e.id, e.own[index], index, False,
                                               **self._callbacks()))
            return
        spec = next((f for f in self.service.global_fields() if f.id == key[1]), None)
        if spec is None:
            return
        n_entries = len(self.service.list_entries())
        if not confirm(self, "Remove a shared field",
                       f"“{name}” is shared by all {n_entries} entries of this database.\n\n"
                       "Removing it takes the field off every entry. Entries that have text in it keep "
                       "that text as a field of their own.", "Remove from all entries",
                       destructive=True):
            return
        removed = undo.RemovedField(spec, 0)
        self._push(undo.SharedFieldChange(self.service, e.id, removed, False, **self._callbacks()))

    # --- photos ---------------------------------------------------------------------------------------------

    def _load_photos(self) -> None:
        keep = {i.data(ROLE_ID) for i in self.photos.selectedItems()}     # kept selected across a refresh
        current = self.photos.currentItem().data(ROLE_ID) if self.photos.currentItem() else None
        self.photos.blockSignals(True)
        self.photos.clear()
        e = self.entry
        small = []                                         # thumbnails made smaller by an older version
        for i, ref in enumerate(e.photos):
            excluded = ref.face is None
            item = QListWidgetItem()
            try:
                pm = bytes_to_pixmap(self.service.thumb_bytes(ref.photo_id, ref.face))
            except Exception:
                pm = QPixmap()
            if pm.isNull():
                pm = placeholder_pixmap(PHOTO_ICON)
            elif pm.width() < config.THUMB_SIZE:
                small.append(ref.photo_id)
            size = pm.width()                              # draw marks in the thumbnail's own pixels
            if excluded:
                pm = mark_excluded(pm, PHOTO_ICON)
            pm = rounded(pm, round(PHOTO_RADIUS * size / PHOTO_ICON))
            if i == 0:
                pm = mark_cover(pm, PHOTO_ICON, PHOTO_RADIUS)
            item.setIcon(QIcon(pm))
            item.setData(ROLE_ID, ref.photo_id)
            item.setData(ROLE_COVER, i == 0)
            origin = "added from a search" if ref.from_query else "imported"
            tip = f"Added {fmt_time(ref.added_at)}, {origin}."
            if i == 0:
                tip += "\nThe first photo is the entry's cover."
            if excluded:
                tip += ("\nNot used for matching" + (" – no face was found." if ref.face_count == 0 else
                                                     " (you switched it off)."))
            elif ref.face_count > 1:
                tip += f"\n{ref.face_count} faces found."
            item.setToolTip(tip)
            self.photos.addItem(item)
        rows = [i for i, ref in enumerate(e.photos) if ref.photo_id in keep]
        select_rows(self.photos, rows, next((r for r in rows if e.photos[r].photo_id == current),
                                            rows[0]) if rows else None)
        self.photos.blockSignals(False)
        self.photos_title.setText(f"Photos ({len(e.photos)})")
        self._update_photo_buttons()
        if small:
            self._sharpen_thumbs(e.id, small)

    def _sharpen_thumbs(self, entry_id: str, small: list[str]) -> None:
        """Make the small thumbnails again at today's size, in the background, then show them."""
        if self.runner.busy:
            return

        def done(n):
            if n and self.entry is not None and self.entry.id == entry_id:
                self._load_photos()

        self.runner.submit(lambda: self.service.remake_thumbs(entry_id, small), on_done=done)

    def _photos_reordered(self, order: list) -> None:
        """A drag in the album: saved at once (⌘Z puts the old order back)."""
        if self.entry is None:
            return
        old = [p.photo_id for p in self.entry.photos]
        if set(order) != set(old) or list(order) == old:
            return
        self.commit_edits()
        self._push(undo.ReorderPhotos(self.service, self.entry.id, old, list(order), **self._callbacks()))

    def _update_photo_buttons(self) -> None:
        refs = self._selected_photos()
        off = bool(refs) and all(r.face is None for r in refs)          # none of them is used for matching
        self.remove_photo_btn.setEnabled(bool(refs))
        self.exclude_btn.setEnabled(any(r.face_count > 0 for r in refs) if off else bool(refs))
        self.exclude_btn.setChecked(off)
        what = "the selected photos" if len(refs) > 1 else "the selected photo"
        self.remove_photo_btn.setToolTip(f"Move {what} to the Trash")
        self.exclude_btn.setToolTip(f"Use {what} for matching again" if off else f"Do not use {what} for matching")

    def _selected_photos(self) -> list:
        """The selected photos of the album, in album order."""
        if self.entry is None:
            return []
        refs = [self.entry.photo(i.data(ROLE_ID)) for i in selected_items(self.photos)]
        return [r for r in refs if r is not None]

    def _current_photo(self):
        item = self.photos.currentItem()
        if item is None or self.entry is None:
            return None
        return self.entry.photo(item.data(ROLE_ID))

    def add_photos(self) -> None:
        if self.entry is None:
            return
        self.commit_edits()
        paths = open_images(self, "Add photos to this entry", kind="album")
        if paths:
            self.add_photo_files(self.entry.id, [Path(p) for p in paths])

    def add_photo_files(self, entry_id: str, paths: list) -> None:
        """Add photos to an album: files (Path) or image data (bytes), dropped in or chosen."""
        self.commit_edits()
        dlg = QProgressDialog("Adding photos…", "Cancel", 0, len(paths), self)
        dlg.setWindowModality(Qt.WindowModality.WindowModal)
        dlg.setMinimumDuration(400)

        def work(progress, cancel):
            added, dup, noface, errors = [], 0, 0, 0
            for i, p in enumerate(paths):
                if cancel():
                    break
                try:
                    res = self.service.add_photo(entry_id, p if isinstance(p, bytes) else p.read_bytes())
                except OSError:
                    errors += 1
                    continue
                if res.error:
                    errors += 1
                elif res.duplicate:
                    dup += 1
                else:
                    added.append(res.photo.photo_id)
                    noface += res.face_count == 0
                progress(i + 1, len(paths))
            return added, dup, noface, errors

        def done(out):
            dlg.close()
            added, dup, noface, errors = out
            if added:
                self._push(undo.AddPhotos(self.service, entry_id, added, **self._callbacks()))
            self.refresh(entry_id)
            self.changed.emit()
            notes = []
            if noface:
                notes.append(f"{noface} without a detectable face (kept, but not used for matching).")
            if dup:
                notes.append(f"{dup} already in the album (skipped).")
            if errors:
                notes.append(f"{errors} could not be read.")
            if notes:                              # the new photos show up in the album by themselves
                QMessageBox.information(self, "Add photos", " ".join([f"Added {len(added)} photo(s)."] + notes))

        task = self.runner.submit(work, on_done=done, on_progress=lambda d, t: dlg.setValue(d), with_progress=True,
                                  on_error=lambda exc: (dlg.close(), default_error_handler(exc)))
        dlg.canceled.connect(task.cancel)

    def open_photo(self) -> None:
        """Double-click on a photo: show it big, with its faces; double-clicking a face picks the one to
        match on."""
        ref = self._current_photo()
        if ref is None or self.entry is None:
            return
        self.commit_edits()
        entry_id = self.entry.id

        def done(out):
            img, faces = out
            dlg = PhotoDialog(self.service, self.runner, entry_id, ref, img, faces, self)
            dlg.face_changed.connect(lambda pid, old, new: self._faces_changed(entry_id, [(pid, old, new)]))
            dlg.save_note = lambda pid, old, new: self._push(
                undo.SetPhotoNote(self.service, entry_id, pid, old, new, **self._callbacks()), light=True)
            dlg.exec()

        self.runner.submit(lambda: self.service.detect_faces(ref.photo_id), on_done=done)

    def _faces_changed(self, entry_id: str, changes: list[tuple[str, Face | None, Face | None]]) -> None:
        self._push(undo.ChooseFaces(self.service, entry_id, changes, **self._callbacks()))
        self.refresh(entry_id)
        self.changed.emit()

    def toggle_excluded(self) -> None:
        """The 🚫 button: stop using the selected photos for matching or, when none of them is used,
        start using them again (with their largest face)."""
        refs = self._selected_photos()
        if not refs or self.entry is None:
            self.exclude_btn.setChecked(False)
            return
        self.commit_edits()
        entry_id = self.entry.id
        include = all(r.face is None for r in refs)
        todo = [(r.photo_id, r.face) for r in refs if (r.face_count > 0 if include else r.face is not None)]

        def work():
            changes = []
            for photo_id, before in todo:
                if include:
                    _img, faces = self.service.detect_faces(photo_id)
                    if not faces:
                        continue
                    ref = self.service.choose_face(entry_id, photo_id, faces[0])       # the largest face
                else:
                    ref = self.service.choose_face(entry_id, photo_id, None)
                changes.append((photo_id, before, ref.face))
            return changes

        def done(changes):
            if not changes:
                QMessageBox.information(self, "Photo", "No face could be found in this photo, so it cannot be used "
                                        "for matching." if len(todo) <= 1 else "No face could be found in these "
                                        "photos, so they cannot be used for matching.")
                self.refresh(entry_id)
                return
            self._faces_changed(entry_id, changes)

        self.runner.submit(work, on_done=done)

    def remove_photo(self) -> None:
        refs = self._selected_photos()
        if not refs:
            return
        text = ("Move this photo to the Trash? You can restore it later." if len(refs) == 1 else
                f"Move these {len(refs)} photos to the Trash? You can restore them later.")
        if confirm(self, "Remove photos" if len(refs) > 1 else "Remove photo", text, "Move to Trash"):
            self.commit_edits()
            self._push(undo.RemovePhotos(self.service, self.entry.id, [r.photo_id for r in refs],
                                         **self._callbacks()))

    # --- entry actions ------------------------------------------------------------------------------------

    def new_entry(self) -> None:
        self.commit_edits()
        name, ok = QInputDialog.getText(self, "New entry", "Name:")
        if ok and name.strip():
            e = self.service.create_entry(name.strip())
            self._push(undo.NewEntry(self.service, e.id, **self._callbacks()))
            self.refresh(e.id)
            self.select_entry(e.id)
            self.changed.emit()

    def delete_entry(self) -> None:
        """Move the selected entries (or the one shown) to the Trash, as one step to undo."""
        ids = self._targets()
        if not ids:
            return
        self.commit_edits()
        entries = [self.service.get_entry(eid) for eid in ids]
        photos = sum(len(e.photos) for e in entries)
        if len(entries) == 1:
            title, text = "Delete entry", (f"Move “{entries[0].name}” and its {photos} photo(s) to the Trash?\n"
                                           "You can restore it from the Trash.")
        else:
            title, text = "Delete entries", (f"Move these {len(entries)} entries and their {photos} photo(s) to "
                                             "the Trash?\nYou can restore them from the Trash.")
        if confirm(self, title, text, "Move to Trash", destructive=True):
            self._push(undo.DeleteEntries(self.service, ids, **self._callbacks()))

    def export_entry(self) -> None:
        """Unencrypted copies of the selected entries (or the one shown), one folder each."""
        ids = self._targets()
        if not ids:
            return
        self.commit_edits()
        what = "this entry's" if len(ids) == 1 else f"these {len(ids)} entries'"
        if not confirm(self, "Export entry" if len(ids) == 1 else "Export entries",
                       f"Export creates UNENCRYPTED copies of {what} information and photos in a folder "
                       "you choose. Anyone with access to that folder can see them.\n\nContinue?",
                       "Choose folder…", destructive=True):
            return
        folder = choose_folder(self, "Export to folder")
        if folder:
            outs = [self.service.export_entry(eid, Path(folder)) for eid in ids]
            QMessageBox.information(self, "Export", f"Exported to:\n{outs[0] if len(outs) == 1 else folder}")

    def import_folder(self) -> None:
        self.commit_edits()
        text = ("Choose a folder that contains one sub-folder per person. Each sub-folder holds that person's "
                "photos and, optionally, an entry.json with fields such as name, aliases, tags and notes.\n\n"
                "A folder that directly contains photos is imported as one person. Photos are copied into the "
                "encrypted database; the original files are not changed, moved or deleted.")
        if not confirm(self, "Import folder", text, "Choose folder…"):
            return
        folder = choose_folder(self, "Import from folder")
        if not folder:
            return
        dlg = QProgressDialog("Importing…", "Cancel", 0, 0, self)
        dlg.setWindowModality(Qt.WindowModality.WindowModal)
        dlg.setMinimumDuration(300)

        def progress(d, t):
            dlg.setMaximum(t)
            dlg.setValue(d)

        def done(rep):
            dlg.close()
            self.refresh()
            self.changed.emit()
            msg = (f"New entries: {rep.entries_created}\nExisting entries extended: {rep.entries_updated}\n"
                   f"Photos added: {rep.photos_added}\nDuplicates skipped: {rep.duplicates}\n"
                   f"Photos without a face: {rep.no_face}\nPhotos with several faces: {rep.multi_face}")
            if rep.errors:
                msg += f"\nProblems: {len(rep.errors)}\n\n" + "\n".join(rep.errors[:8])
            QMessageBox.information(self, "Import finished", msg)

        task = self.runner.submit(lambda p, c: self.service.import_folder(Path(folder), p, c), on_done=done,
                                  on_progress=progress, with_progress=True,
                                  on_error=lambda exc: (dlg.close(), default_error_handler(exc)))
        dlg.canceled.connect(task.cancel)

    # --- misc -------------------------------------------------------------------------------------------------

    def clear(self) -> None:
        """Forget everything shown, and the undo history (locking, or another database)."""
        if self._editing():
            self._active.finish(False)
        self._active = None
        self.undo_stack.clear()
        self.entry = None
        self._body_key = None
        self.list.clear()
        self.photos.clear()
        self.filter.clear()
        for inline in (self.title, self.aliases_text, self.tags_text):
            inline.set_text("")
            inline.editor.clear()
        self.count.setText("")
        self.stamps.setText("")
        self._rows, self._divider = {}, None
        while self.body.count() > 1:
            item = self.body.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._show_entry(False)
