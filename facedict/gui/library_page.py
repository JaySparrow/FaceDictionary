"""Library page: the list of entries, an entry's page (read-only, or in edit mode), and its album."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFontMetrics, QIcon, QPainter, QPen
from PySide6.QtWidgets import (QAbstractItemView, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QFrame,
                               QHBoxLayout, QInputDialog, QLabel, QLineEdit, QListView, QListWidget,
                               QListWidgetItem, QMessageBox, QPlainTextEdit, QProgressDialog, QPushButton,
                               QRadioButton, QScrollArea, QSplitter, QStackedWidget, QVBoxLayout, QWidget)

from ..engine import iou, reading_order
from ..models import Entry, Face, OwnField, fold, split_values
from ..service import SCOPE_ALL, SCOPE_NAME, SCOPE_TAGS, haystacks
from .common import (ElidedLabel, add_copy_action, bytes_to_pixmap, choose_folder, confirm,
                     default_error_handler, divider, header_row, heading, icon_button, make_labels_selectable,
                     mark_excluded, muted, open_images, placeholder_pixmap)
from .faces_view import QueryView, render_photo
from .history_page import fmt_time

ROLE_ID = Qt.ItemDataRole.UserRole
ROLE_SEARCH = Qt.ItemDataRole.UserRole + 1
PHOTO_ICON = 116
LABEL_WIDTH = 110


def split_list(text: str) -> list[str]:
    return split_values(text)


# --- small widgets -------------------------------------------------------------------------------

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


class FieldRow(QWidget):
    """One row of an entry's page: the field's name on the left, its text on the right."""

    remove_clicked = Signal()

    def __init__(self, name: str, value: str, personal: bool, editable: bool, parent=None):
        super().__init__(parent)
        self.field_name = name
        label = QLabel(name)
        label.setWordWrap(True)
        label.setFixedWidth(LABEL_WIDTH)
        label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        f = label.font()
        f.setBold(True)
        label.setFont(f)
        label.setToolTip("A field of this entry only" if personal
                         else "A field shared by every entry of this database")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)
        lay.addWidget(label)
        if editable:
            self.editor = GrowingTextEdit(value)
            lay.addWidget(self.editor, 1)
            minus = icon_button("–", f"Remove “{name}”", 24)
            minus.clicked.connect(self.remove_clicked.emit)
            lay.addWidget(minus, 0, Qt.AlignmentFlag.AlignTop)
        else:
            self.editor = None
            text = QLabel(value)
            text.setWordWrap(True)
            text.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            lay.addWidget(text, 1)

    def value(self) -> str:
        return self.editor.toPlainText() if self.editor is not None else ""


class PhotoGrid(QListWidget):
    """The album grid. Photos always snap to the grid and pack to the left; dragging one either
    inserts it between two photos or swaps it with the photo it is dropped on."""

    reordered = Signal(list)                       # photo ids in the new order
    DROP_COLOR = QColor(59, 130, 246)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setViewMode(QListView.ViewMode.IconMode)
        self.setMovement(QListView.Movement.Static)          # items snap back into the grid
        self.setResizeMode(QListView.ResizeMode.Adjust)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setDropIndicatorShown(False)                    # we draw our own
        self.setDragDropMode(QAbstractItemView.DragDropMode.NoDragDrop)
        self._drag_row = -1
        self._drop: tuple[str, int] | None = None

    def set_reorderable(self, on: bool) -> None:
        self.setDragEnabled(on)
        self.viewport().setAcceptDrops(on)
        self.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove if on
                             else QAbstractItemView.DragDropMode.NoDragDrop)

    def ids(self) -> list[str]:
        return [self.item(i).data(ROLE_ID) for i in range(self.count())]

    def _rect(self, row: int):
        return self.visualRect(self.indexFromItem(self.item(row)))

    # --- what a drop at this point would do -------------------------------------------------

    def target_at(self, pos) -> tuple[str, int]:
        index = self.indexAt(pos)
        if index.isValid():
            rect = self.visualRect(index)
            frac = (pos.x() - rect.left()) / max(1, rect.width())
            if 0.3 <= frac <= 0.7:
                return "swap", index.row()
            return "insert", index.row() + (0 if frac < 0.3 else 1)
        for i in range(self.count()):
            rect = self._rect(i)
            if pos.y() < rect.top():
                return "insert", i
            if rect.top() <= pos.y() <= rect.bottom() and pos.x() < rect.center().x():
                return "insert", i
        return "insert", self.count()

    @staticmethod
    def new_order(ids: list[str], source: int, kind: str, target: int) -> list[str]:
        ids = list(ids)
        if kind == "swap":
            if target != source:
                ids[source], ids[target] = ids[target], ids[source]
            return ids
        moved = ids.pop(source)
        ids.insert(target - 1 if target > source else target, moved)
        return ids

    # --- drag and drop ------------------------------------------------------------------------

    def startDrag(self, actions):
        self._drag_row = self.currentRow()
        try:
            super().startDrag(actions)
        finally:
            self._drag_row = -1
            self._drop = None
            self.viewport().update()

    def dragEnterEvent(self, event):
        if self._drag_row >= 0:
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        if self._drag_row < 0:
            return event.ignore()
        self._drop = self.target_at(event.position().toPoint())
        event.setDropAction(Qt.DropAction.MoveAction)
        event.accept()
        self.viewport().update()

    def dragLeaveEvent(self, event):
        self._drop = None
        self.viewport().update()
        super().dragLeaveEvent(event)

    def dropEvent(self, event):
        drop, source = self._drop, self._drag_row
        self._drop = None
        self.viewport().update()
        if drop is None or source < 0:
            return event.ignore()
        order = self.new_order(self.ids(), source, *drop)
        event.setDropAction(Qt.DropAction.IgnoreAction)      # the page rebuilds the grid itself
        event.accept()
        if order != self.ids():
            QTimer.singleShot(0, lambda: self.reordered.emit(order))

    # --- the blue hint that tells insert from swap -----------------------------------------------

    def paintEvent(self, event):
        super().paintEvent(event)
        if self._drop is None or not self.count():
            return
        kind, target = self._drop
        painter = QPainter(self.viewport())
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        color = self.DROP_COLOR
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


class AddFieldDialog(QDialog):
    """Ask for a new field: shared by the whole database, or only on this entry."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Add a field")
        self.name = QLineEdit()
        self.name.setPlaceholderText("Field name, e.g. “Met at” or “Phone”")
        self.shared = QRadioButton("Shared field – every entry of this database gets it")
        self.personal = QRadioButton("Only this entry")
        self.shared.setChecked(True)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel("Name of the new field:"))
        lay.addWidget(self.name)
        lay.addWidget(self.shared)
        lay.addWidget(self.personal)
        lay.addWidget(buttons)
        self.setMinimumWidth(420)

    def values(self) -> tuple[str, bool]:
        return self.name.text().strip(), self.shared.isChecked()


class PhotoDialog(QDialog):
    """A photo of an album, with its faces. Clicking a face makes it the one used for matching."""

    changed = Signal()

    def __init__(self, service, runner, entry_id: str, photo_ref, image, faces: list[Face],
                 editable: bool = False, parent=None):
        super().__init__(parent)
        self.service = service
        self.runner = runner
        self.entry_id = entry_id
        self.ref = photo_ref
        self.editable = editable
        self.faces = [faces[i] for i in reading_order(faces)]
        self.setWindowTitle("Photo")
        self.resize(820, 700)

        current = -1
        if photo_ref.face is not None and self.faces:
            overlaps = [iou(photo_ref.face, f) for f in self.faces]
            current = max(range(len(self.faces)), key=overlaps.__getitem__) if max(overlaps) > 0.5 else -1
        interactive = editable and len(self.faces) > 1
        self.view = QueryView(interactive=interactive, hint="Click a face to use that one for matching.")
        self.view.image.hover_text = "Face {n} – click to use it for matching"
        self.view.show_query(render_photo(image, self.faces), self.faces, current)
        if interactive:
            self.view.face_selected.connect(self._apply)

        self.status = muted(self._status_text(current))
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        lay = QVBoxLayout(self)
        lay.addWidget(self.view, 1)
        lay.addWidget(self.status)
        lay.addWidget(buttons)
        make_labels_selectable(self)

    def _status_text(self, selected: int) -> str:
        where = "added from a search" if self.ref.from_query else "imported"
        base = f"Added {fmt_time(self.ref.added_at)}, {where}."
        if not self.faces:
            return base + " No face was found in this photo, so it is not used for matching."
        if selected < 0:
            return base + " This photo is not used for matching – click a face to use it."
        if len(self.faces) == 1:
            return base + " The face in the green box is used for matching."
        if self.editable:
            return base + f" Face {selected + 1} (green) is used for matching."
        return base + (f" Face {selected + 1} (green) is used for matching – edit the entry (✏️) to "
                       "pick another one.")

    def _apply(self, index: int) -> None:
        self.status.setText("Saving…")
        photo_id = self.ref.photo_id

        def done(ref):
            self.ref = ref
            self.status.setText(self._status_text(index))
            self.changed.emit()

        def failed(exc):
            self.status.setText(str(exc))

        self.runner.submit(lambda: self.service.choose_face(self.entry_id, photo_id, self.faces[index]),
                           on_done=done, on_error=failed)


# --- page ----------------------------------------------------------------------------------------

class LibraryPage(QWidget):
    changed = Signal()

    def __init__(self, service, runner, parent=None):
        super().__init__(parent)
        self.service = service
        self.runner = runner
        self.entry: Entry | None = None
        self.editing = False
        self._dirty = False
        self._loading = False
        self._pending_order: list[str] | None = None          # photo order waiting to be saved
        self._rows: list[tuple[str, str, FieldRow]] = []      # (kind, id-or-name, row) while editing

        split = QSplitter()
        split.addWidget(self._build_list())
        split.addWidget(self._build_entry())
        split.setStretchFactor(0, 2)
        split.setStretchFactor(1, 5)
        split.setChildrenCollapsible(False)
        split.setSizes([300, 950])
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 16, 16, 16)
        lay.addWidget(split)
        self._show_entry(False)

    # --- left: the list of entries ------------------------------------------------------------

    def _build_list(self) -> QWidget:
        self.scope = QComboBox()
        self.scope.setToolTip("Where to search")
        self.scope.currentIndexChanged.connect(self._apply_filter)
        self.filter = QLineEdit()
        self.filter.setPlaceholderText("Search the library")
        self.filter.setClearButtonEnabled(True)
        self.filter.textChanged.connect(self._apply_filter)
        search_row = QHBoxLayout()
        search_row.setSpacing(4)
        search_row.addWidget(self.scope)
        search_row.addWidget(self.filter, 1)

        self.list = QListWidget()
        self.list.setIconSize(QSize(40, 40))
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.list.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.list.currentItemChanged.connect(self._entry_selected)
        add_copy_action(self.list)
        self.count = muted()
        new_btn = icon_button("+", "New entry (⌘N)")
        new_btn.clicked.connect(self.new_entry)
        import_btn = QPushButton("Import…")
        import_btn.setToolTip("Import a folder that holds one sub-folder per person")
        import_btn.clicked.connect(self.import_folder)

        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        ll.setSpacing(6)
        ll.addWidget(header_row("Library", new_btn, import_btn))
        ll.addLayout(search_row)
        ll.addWidget(self.list, 1)
        ll.addWidget(self.count)
        return left

    # --- right: one entry --------------------------------------------------------------------------

    def _build_entry(self) -> QWidget:
        self.title = heading("", 17)
        self.title.setWordWrap(False)
        self.aliases_label = ElidedLabel()
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("Name")
        self.name_edit.textEdited.connect(self._mark_dirty)
        self.edit_btn = icon_button("✏️", "Edit this entry")
        self.edit_btn.clicked.connect(lambda: self.set_editing(True))
        self.save_btn = icon_button("✓", "Save the changes")
        self.save_btn.clicked.connect(self.save)
        self.cancel_btn = icon_button("↩", "Discard the changes")
        self.cancel_btn.clicked.connect(lambda: self.set_editing(False, discard=True))
        self.export_btn = QPushButton("Export…")
        self.export_btn.setToolTip("Write an unencrypted copy of this entry into a folder")
        self.export_btn.clicked.connect(self.export_entry)
        self.delete_btn = icon_button("🗑", "Move this entry and its photos to the Trash")
        self.delete_btn.clicked.connect(self.delete_entry)
        header = header_row("", self.edit_btn, self.save_btn, self.cancel_btn, self.export_btn, self.delete_btn)
        header.layout().insertWidget(0, self.title)
        header.layout().insertWidget(1, self.aliases_label, 1)
        header.layout().insertWidget(2, self.name_edit, 1)

        self.tags_label = muted()
        self.view_body = QVBoxLayout()
        self.view_body.setSpacing(6)
        self.edit_body = QVBoxLayout()
        self.edit_body.setSpacing(6)
        self.pages = QStackedWidget()
        for body in (self.view_body, self.edit_body):
            holder = QWidget()
            holder.setLayout(body)
            body.addStretch(1)
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setFrameShape(QFrame.Shape.NoFrame)
            scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            scroll.setWidget(holder)
            self.pages.addWidget(scroll)

        self.photos_title = heading("Photos", 13)
        self.add_photo_btn = icon_button("+", "Add photos to this entry")
        self.add_photo_btn.clicked.connect(self.add_photos)
        self.remove_photo_btn = icon_button("–", "Move the selected photo to the Trash")
        self.remove_photo_btn.clicked.connect(self.remove_photo)
        self.exclude_btn = icon_button("🚫", "Do not use the selected photo for matching", checkable=True)
        self.exclude_btn.clicked.connect(self.toggle_excluded)
        self.photo_hint = muted()
        photos_header = QHBoxLayout()
        photos_header.addWidget(self.photos_title)
        photos_header.addSpacing(8)
        photos_header.addWidget(self.photo_hint, 1)
        for b in (self.add_photo_btn, self.remove_photo_btn, self.exclude_btn):
            photos_header.addWidget(b)

        self.photos = PhotoGrid()
        self.photos.setIconSize(QSize(PHOTO_ICON, PHOTO_ICON))
        self.photos.setGridSize(QSize(PHOTO_ICON + 16, PHOTO_ICON + 34))
        self.photos.setMinimumHeight(PHOTO_ICON + 50)
        self.photos.itemDoubleClicked.connect(lambda _i: self.open_photo())
        self.photos.currentItemChanged.connect(lambda *_: self._update_photo_buttons())
        self.photos.reordered.connect(self._photos_reordered)

        self.stamps = muted()

        self.entry_widget = QWidget()
        el = self.entry_layout = QVBoxLayout(self.entry_widget)
        el.setContentsMargins(0, 0, 0, 0)
        el.setSpacing(6)
        el.addWidget(header)
        el.addWidget(self.tags_label)
        el.addWidget(self.pages, 3)
        el.addLayout(photos_header)
        el.addWidget(self.photos, 4)
        el.addWidget(self.stamps)

        self.placeholder = muted("Select an entry, create a new one, or import a folder.")
        self.placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.addWidget(self.placeholder)
        rl.addWidget(self.entry_widget, 1)
        return right

    # --- list handling -------------------------------------------------------------------------------

    def refresh(self, select_id: str | None = None) -> None:
        if not self.service.unlocked:
            return
        current = select_id or (self.entry.id if self.entry else None)
        entries = self.service.list_entries()
        field_ids = [f.id for f in self.service.global_fields()]
        self._fill_scopes()
        self.list.blockSignals(True)
        self.list.clear()
        for e in entries:
            item = QListWidgetItem(f"{e.name}\n{len(e.photos)} photo(s)")
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
            if e.id == current:
                self.list.setCurrentItem(item)
        self.list.blockSignals(False)
        self.count.setText(f"{len(entries)} entries · {sum(len(e.photos) for e in entries)} photos")
        self._apply_filter()
        if current and any(e.id == current for e in entries):
            if not self._dirty and not self.editing:          # never interrupt an edit in progress
                self._load_entry(current)
        else:
            self._load_entry(None)

    def _fill_scopes(self) -> None:
        wanted = [("All fields", SCOPE_ALL), ("Name", SCOPE_NAME), ("Tags", SCOPE_TAGS)]
        wanted += [(f.name, f.id) for f in self.service.global_fields()]
        if [self.scope.itemData(i) for i in range(self.scope.count())] == [w[1] for w in wanted]:
            return
        keep = self.scope.currentData()
        self.scope.blockSignals(True)
        self.scope.clear()
        for label, data in wanted:
            self.scope.addItem(label, data)
        index = self.scope.findData(keep)
        self.scope.setCurrentIndex(index if index >= 0 else 0)
        self.scope.blockSignals(False)

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
                self.list.setCurrentRow(i)
                return

    def _entry_selected(self, item: QListWidgetItem | None, previous: QListWidgetItem | None) -> None:
        if self._dirty and self.entry is not None and not self._resolve_unsaved():
            self.list.blockSignals(True)
            self.list.setCurrentItem(previous)
            self.list.blockSignals(False)
            return
        self._load_entry(item.data(ROLE_ID) if item else None)

    def _resolve_unsaved(self) -> bool:
        box = QMessageBox(self)
        box.setWindowTitle("Unsaved changes")
        box.setText(f"Save the changes to “{self.entry.name}”?")
        box.setStandardButtons(QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Discard
                               | QMessageBox.StandardButton.Cancel)
        choice = box.exec()
        if choice == QMessageBox.StandardButton.Save:
            return self.save()
        return choice == QMessageBox.StandardButton.Discard

    # --- showing one entry ------------------------------------------------------------------------------

    def _show_entry(self, on: bool) -> None:
        self.entry_widget.setVisible(on)
        self.placeholder.setVisible(not on)

    def _load_entry(self, entry_id: str | None) -> None:
        self._loading = True
        try:
            self.entry = self.service.get_entry(entry_id) if entry_id and self.service.unlocked else None
            self._show_entry(self.entry is not None)
            if self.entry is None:
                self.photos.clear()
                return
            self.set_editing(False, reload=True)
            self._load_photos()
        finally:
            self._loading = False
            self._set_dirty(False)

    def set_editing(self, editing: bool, discard: bool = False, reload: bool = False) -> None:
        """Switch between the read-only page and the edit form."""
        if self.entry is None:
            return
        if not editing and self._dirty and not discard and not reload:
            if not self._resolve_unsaved():
                return
        if discard or reload:
            self._pending_order = None
            self.entry = self.service.get_entry(self.entry.id)
            self._set_dirty(False)
            self._load_photos()
        self.editing = editing
        self.pages.setCurrentIndex(1 if editing else 0)
        self.title.setVisible(not editing)
        self.aliases_label.setVisible(not editing)
        self.tags_label.setVisible(not editing)
        self.name_edit.setVisible(editing)
        self.edit_btn.setVisible(not editing)
        self.save_btn.setVisible(editing)
        self.cancel_btn.setVisible(editing)
        for b in (self.add_photo_btn, self.remove_photo_btn, self.exclude_btn):
            b.setVisible(editing)
        self.export_btn.setVisible(not editing)
        self.delete_btn.setVisible(not editing)
        self.photos.set_reorderable(editing)
        self.entry_layout.setStretch(2, 5 if editing else 3)          # the text part
        self.entry_layout.setStretch(4, 3 if editing else 4)          # the album
        self.photo_hint.setText("Drag a photo between two others to insert it, or onto one to swap them – "
                                "the first photo is the cover." if editing else "")
        self._build_body()

    def _build_body(self) -> None:
        loading, self._loading = self._loading, True
        try:
            body = self.edit_body if self.editing else self.view_body
            while body.count() > 1:                                  # keep the trailing stretch
                item = body.takeAt(0)
                if item.widget():
                    item.widget().deleteLater()
            self._rows = []
            e = self.entry
            self.title.setText(e.name)
            self.name_edit.setText(e.name)
            self.aliases_label.setText(("  ·  " + "  ·  ".join(e.aliases)) if e.aliases else "")
            self.tags_label.setText("  ·  ".join(e.tags))
            self.tags_label.setVisible(bool(e.tags) and not self.editing)
            self.stamps.setText(f"Created {fmt_time(e.created_at)}  ·  updated {fmt_time(e.updated_at)}")
            if self.editing:
                self._build_edit_body(body, e)
            else:
                self._build_view_body(body, e)
            make_labels_selectable(self)
        finally:
            self._loading = loading

    def _build_view_body(self, body: QVBoxLayout, e: Entry) -> None:
        shown = 0
        for spec in self.service.global_fields():
            text = e.fields.get(spec.id, "").strip()
            if text:
                body.insertWidget(body.count() - 1, FieldRow(spec.name, text, False, False))
                shown += 1
        own = [f for f in e.own if f.value.strip()]
        if own:
            body.insertWidget(body.count() - 1, divider("only in this entry"))
            for f in own:
                body.insertWidget(body.count() - 1, FieldRow(f.name, f.value, True, False))
                shown += 1
        if not shown:
            body.insertWidget(body.count() - 1, muted("No information yet – press the pencil to add some."))

    def _build_edit_body(self, body: QVBoxLayout, e: Entry) -> None:
        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        self.aliases = QLineEdit(", ".join(e.aliases))
        self.aliases.setPlaceholderText("Other names, separated by commas")
        self.tags = QLineEdit(", ".join(e.tags))
        self.tags.setPlaceholderText("Tags, separated by commas")
        for w in (self.aliases, self.tags):
            w.textEdited.connect(self._mark_dirty)
        form.addRow("Aliases", self.aliases)
        form.addRow("Tags", self.tags)
        holder = QWidget()
        holder.setLayout(form)
        body.insertWidget(body.count() - 1, holder)

        for spec in self.service.global_fields():
            self._add_row(body, "global", spec.id, spec.name, e.fields.get(spec.id, ""))
        if e.own:
            body.insertWidget(body.count() - 1, divider("only in this entry"))
        for own in e.own:
            self._add_row(body, "own", own.name, own.name, own.value)

        add_btn = QPushButton("+ Add a field…")
        add_btn.setToolTip("A field shared by every entry, or one just for this entry")
        add_btn.clicked.connect(self.add_field)
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(add_btn)
        wrap = QWidget()
        wrap.setLayout(row)
        body.insertWidget(body.count() - 1, wrap)

    def _add_row(self, body: QVBoxLayout, kind: str, key: str, name: str, value: str) -> None:
        row = FieldRow(name, value, kind == "own", True)
        row.editor.textChanged.connect(self._mark_dirty)
        row.remove_clicked.connect(lambda k=kind, key=key, name=name: self.remove_field(k, key, name))
        body.insertWidget(body.count() - 1, row)
        self._rows.append((kind, key, row))

    # --- editing ------------------------------------------------------------------------------------------

    def _mark_dirty(self, *_args) -> None:
        if not self._loading:
            self._set_dirty(True)

    def _set_dirty(self, dirty: bool) -> None:
        self._dirty = dirty

    def collect(self) -> Entry:
        """The entry as the form currently shows it."""
        e = self.service.get_entry(self.entry.id)
        e.name = self.name_edit.text().strip()
        e.aliases = split_list(self.aliases.text())
        e.tags = split_list(self.tags.text())
        e.fields = {key: row.value() for kind, key, row in self._rows if kind == "global"}
        e.own = [OwnField(key, row.value()) for kind, key, row in self._rows if kind == "own"]
        return e

    def save(self) -> bool:
        if self.entry is None or not self.editing:
            return True
        edited = self.collect()
        if not edited.name:
            QMessageBox.warning(self, "Save", "The entry needs a name.")
            return False
        self._flush_photo_order()
        self.service.update_entry(edited)
        self._set_dirty(False)
        self.entry = self.service.get_entry(edited.id)
        self.set_editing(False)
        self.refresh(edited.id)
        self.changed.emit()
        return True

    def add_field(self) -> None:
        dlg = AddFieldDialog(self)
        if not dlg.exec():
            return
        name, shared = dlg.values()
        if not name:
            return
        if shared:
            try:
                spec = self.service.add_global_field(name)
            except Exception as exc:
                QMessageBox.warning(self, "Add a field", str(exc))
                return
            pending = self.collect()
            self.service.update_entry(pending)
            self.entry = self.service.get_entry(pending.id)
            self._build_body()
            self._fill_scopes()
            self.changed.emit()
            self._focus_row("global", spec.id)
        else:
            if self.entry.own_field(name) or any(fold(f.name) == fold(name)
                                                 for f in self.service.global_fields()):
                QMessageBox.warning(self, "Add a field", f"There is already a field called “{name}”.")
                return
            pending = self.collect()
            pending.own.append(OwnField(name, ""))
            self.entry = pending
            self._build_body()
            self._set_dirty(True)
            self._focus_row("own", name)

    def _focus_row(self, kind: str, key: str) -> None:
        for row_kind, row_key, row in self._rows:
            if row_kind == kind and row_key == key and row.editor is not None:
                row.editor.setFocus()
                return

    def remove_field(self, kind: str, key: str, name: str) -> None:
        if kind == "own":
            pending = self.collect()
            pending.own = [f for f in pending.own if f.name != key]
            self.entry = pending
            self._build_body()
            self._set_dirty(True)
            return
        n_entries = len(self.service.list_entries())
        if not confirm(self, "Remove a shared field",
                       f"“{name}” is shared by all {n_entries} entries of this database.\n\n"
                       "Removing it takes the field off every entry. Entries that have text in it keep "
                       "that text as a field of their own.", "Remove from all entries", destructive=True):
            return
        pending = self.collect()
        self.service.update_entry(pending)
        self.service.remove_global_field(key, keep_content=True)
        self.entry = self.service.get_entry(pending.id)
        self._set_dirty(False)
        self._build_body()
        self._fill_scopes()
        self.refresh(self.entry.id)
        self.changed.emit()

    # --- photos ---------------------------------------------------------------------------------------------

    def _load_photos(self) -> None:
        item = self.photos.currentItem()
        keep = item.data(ROLE_ID) if item else None        # keep the photo selected across a refresh
        self.photos.blockSignals(True)
        self.photos.clear()
        e = self.entry
        for i, ref in enumerate(e.photos):
            excluded = ref.face is None
            label = "cover" if i == 0 else ("from a search" if ref.from_query else "")
            if i > 0 and not excluded and ref.face_count > 1:
                label = f"{ref.face_count} faces"
            item = QListWidgetItem(label)
            try:
                pm = bytes_to_pixmap(self.service.thumb_bytes(ref.photo_id, ref.face))
            except Exception:
                pm = placeholder_pixmap(PHOTO_ICON)
            if excluded:
                pm = mark_excluded(pm, PHOTO_ICON)
            item.setIcon(QIcon(pm))
            item.setData(ROLE_ID, ref.photo_id)
            origin = "added from a search" if ref.from_query else "imported"
            tip = f"Added {fmt_time(ref.added_at)}, {origin}.\nDouble-click to see it big."
            if i == 0:
                tip += "\nThe first photo is the entry's cover."
            if excluded:
                tip += ("\nNot used for matching" + (" – no face was found." if ref.face_count == 0 else
                                                     " (you switched it off)."))
            elif ref.face_count > 1:
                tip += f"\n{ref.face_count} faces found; double-click to check which one is used."
            item.setToolTip(tip)
            self.photos.addItem(item)
            if ref.photo_id == keep:
                self.photos.setCurrentItem(item)
        self.photos.blockSignals(False)
        self.photos_title.setText(f"Photos ({len(e.photos)})")
        self._update_photo_buttons()

    def _photos_reordered(self, order: list) -> None:
        """A drag in the album: remember the new order and stay in edit mode until Save or Discard."""
        if self.entry is None or not self.editing:
            return
        by_id = {p.photo_id: p for p in self.entry.photos}
        if set(order) != set(by_id):
            return
        self.entry.photos = [by_id[pid] for pid in order]
        self._pending_order = list(order)
        self._set_dirty(True)
        self._load_photos()

    def _flush_photo_order(self) -> None:
        if self._pending_order and self.entry is not None:
            self.service.set_photo_order(self.entry.id, self._pending_order)
            self._pending_order = None

    def _update_photo_buttons(self) -> None:
        ref = self._current_photo()
        self.remove_photo_btn.setEnabled(ref is not None)
        self.exclude_btn.setEnabled(ref is not None and not (ref.face is None and ref.face_count == 0))
        self.exclude_btn.setChecked(ref is not None and ref.face is None)
        if ref is not None and ref.face is None:
            self.exclude_btn.setToolTip("Use this photo for matching again")
        else:
            self.exclude_btn.setToolTip("Do not use the selected photo for matching")

    def _current_photo(self):
        item = self.photos.currentItem()
        if item is None or self.entry is None:
            return None
        return self.entry.photo(item.data(ROLE_ID))

    def add_photos(self) -> None:
        if self.entry is None:
            return
        self._flush_photo_order()
        paths = open_images(self, "Add photos to this entry")
        if paths:
            self.add_photo_files(self.entry.id, [Path(p) for p in paths])

    def add_photo_files(self, entry_id: str, paths: list[Path]) -> None:
        dlg = QProgressDialog("Adding photos…", "Cancel", 0, len(paths), self)
        dlg.setWindowModality(Qt.WindowModality.WindowModal)
        dlg.setMinimumDuration(400)

        def work(progress, cancel):
            added = dup = noface = errors = 0
            for i, p in enumerate(paths):
                if cancel():
                    break
                try:
                    res = self.service.add_photo(entry_id, p.read_bytes())
                except OSError:
                    errors += 1
                    continue
                if res.error:
                    errors += 1
                elif res.duplicate:
                    dup += 1
                else:
                    added += 1
                    noface += res.face_count == 0
                progress(i + 1, len(paths))
            return added, dup, noface, errors

        def done(out):
            dlg.close()
            added, dup, noface, errors = out
            parts = [f"Added {added} photo(s)."]
            if noface:
                parts.append(f"{noface} without a detectable face (kept, but not used for matching).")
            if dup:
                parts.append(f"{dup} already in the album (skipped).")
            if errors:
                parts.append(f"{errors} could not be read.")
            self.refresh(entry_id)
            self.changed.emit()
            QMessageBox.information(self, "Add photos", " ".join(parts))

        task = self.runner.submit(work, on_done=done, on_progress=lambda d, t: dlg.setValue(d), with_progress=True,
                                  on_error=lambda exc: (dlg.close(), default_error_handler(exc)))
        dlg.canceled.connect(task.cancel)

    def open_photo(self) -> None:
        """Double-click on a photo: show it big, with its faces; clicking a face picks the one to match on."""
        ref = self._current_photo()
        if ref is None or self.entry is None:
            return
        self._flush_photo_order()
        entry_id = self.entry.id

        editable = self.editing

        def done(out):
            img, faces = out
            dlg = PhotoDialog(self.service, self.runner, entry_id, ref, img, faces, editable, self)
            dlg.changed.connect(lambda: (self.refresh(entry_id), self.changed.emit()))
            dlg.exec()

        self.runner.submit(lambda: self.service.detect_faces(ref.photo_id), on_done=done)

    def toggle_excluded(self) -> None:
        """The 🚫 button: stop using this photo for matching, or start using it again."""
        ref = self._current_photo()
        if ref is None or self.entry is None:
            self.exclude_btn.setChecked(False)
            return
        self._flush_photo_order()
        entry_id, photo_id, excluded = self.entry.id, ref.photo_id, ref.face is None

        def work():
            if not excluded:
                return self.service.choose_face(entry_id, photo_id, None)
            _img, faces = self.service.detect_faces(photo_id)
            if not faces:
                return None
            return self.service.choose_face(entry_id, photo_id, faces[0])       # the largest face

        def done(result):
            if result is None:
                QMessageBox.information(self, "Photo", "No face could be found in this photo, so it cannot be "
                                                       "used for matching.")
            self.entry = self.service.get_entry(entry_id)
            self._load_photos()
            self.refresh(entry_id)
            self.changed.emit()

        self.runner.submit(work, on_done=done)

    def remove_photo(self) -> None:
        ref = self._current_photo()
        if ref is None:
            return
        if confirm(self, "Remove photo", "Move this photo to the Trash? You can restore it later.",
                   "Move to Trash"):
            self._flush_photo_order()
            self.service.remove_photo(self.entry.id, ref.photo_id)
            self.refresh(self.entry.id)
            self.changed.emit()

    # --- entry actions ------------------------------------------------------------------------------------

    def new_entry(self) -> None:
        name, ok = QInputDialog.getText(self, "New entry", "Name:")
        if ok and name.strip():
            e = self.service.create_entry(name.strip())
            self._set_dirty(False)
            self.refresh(e.id)
            self.select_entry(e.id)
            self.set_editing(True)
            self.changed.emit()

    def delete_entry(self) -> None:
        if self.entry is None:
            return
        e = self.entry
        if confirm(self, "Delete entry", f"Move “{e.name}” and its {len(e.photos)} photo(s) to the Trash?\n"
                   "You can restore it from the Trash.", "Move to Trash", destructive=True):
            self.service.delete_entry(e.id)
            self.entry = None
            self._set_dirty(False)
            self.refresh()
            self.changed.emit()

    def export_entry(self) -> None:
        if self.entry is None:
            return
        if not confirm(self, "Export entry",
                       "Export creates UNENCRYPTED copies of this entry's information and photos in a folder "
                       "you choose. Anyone with access to that folder can see them.\n\nContinue?",
                       "Choose folder…", destructive=True):
            return
        folder = choose_folder(self, "Export to folder")
        if folder:
            out = self.service.export_entry(self.entry.id, Path(folder))
            QMessageBox.information(self, "Export", f"Exported to:\n{out}")

    def import_folder(self) -> None:
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
        self.entry = None
        self._dirty = False
        self.editing = False
        self._pending_order = None
        self.list.clear()
        self.photos.clear()
        self.filter.clear()
        self.name_edit.clear()
        self.title.setText("")
        self.count.setText("")
        self._rows = []
        for body in (self.view_body, self.edit_body):
            while body.count() > 1:
                item = body.takeAt(0)
                if item.widget():
                    item.widget().deleteLater()
        self._show_entry(False)

    @property
    def has_unsaved_changes(self) -> bool:
        return self._dirty
