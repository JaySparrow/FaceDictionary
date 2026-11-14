"""Trash page: restore deleted entries, photos and search records, or delete them permanently."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QAbstractItemView, QHeaderView, QMessageBox, QPushButton, QTreeWidget,
                               QTreeWidgetItem, QVBoxLayout, QWidget)

from ..vault import RestoreConflictError
from .common import GAP, PAGE_MARGIN, add_copy_action, confirm, has_focus_in, header_row, muted
from .history_page import fmt_time

ROLE_ID = Qt.ItemDataRole.UserRole
KIND_LABEL = {"entry": "Entry", "photo": "Photo", "history": "Search record"}


class TrashPage(QWidget):
    changed = Signal()

    def __init__(self, service, runner, parent=None):
        super().__init__(parent)
        self.service = service
        self.runner = runner
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Deleted", "Type", "Description"])
        self.tree.setRootIsDecorated(False)
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.tree.header().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.tree.itemSelectionChanged.connect(self._update_buttons)
        add_copy_action(self.tree)
        self.info = muted("Deleted items stay here until you delete them permanently. "
                          "The Trash is never emptied automatically.")
        self.restore_btn = QPushButton("Restore")
        self.restore_btn.clicked.connect(self.restore)
        self.purge_btn = QPushButton("Delete permanently…")
        self.purge_btn.clicked.connect(self.purge)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(PAGE_MARGIN, PAGE_MARGIN, PAGE_MARGIN, PAGE_MARGIN)
        lay.setSpacing(GAP)
        lay.addWidget(header_row("Trash", self.restore_btn, self.purge_btn))
        lay.addWidget(self.info)
        lay.addWidget(self.tree, 1)
        self._update_buttons()

    def refresh(self) -> None:
        if not self.service.unlocked:
            return
        self.tree.clear()
        for item in self.service.list_trash():
            desc = item.label
            if item.kind == "entry":
                desc += f" ({item.payload.get('photo_count', 0)} photo(s))"
            row = QTreeWidgetItem([fmt_time(item.time), KIND_LABEL.get(item.kind, item.kind), desc])
            row.setData(0, ROLE_ID, item.id)
            self.tree.addTopLevelItem(row)
        for c in range(2):
            self.tree.resizeColumnToContents(c)
        self._update_buttons()

    def _selected_ids(self) -> list[str]:
        return [i.data(0, ROLE_ID) for i in self.tree.selectedItems()]

    def _update_buttons(self) -> None:
        has = bool(self.tree.selectedItems())
        self.restore_btn.setEnabled(has)
        self.purge_btn.setEnabled(has)

    def restore(self) -> None:
        ids = self._selected_ids()
        if not ids:
            return
        # restore entries before their photos
        order = {"entry": 0, "history": 1, "photo": 2}
        items = {i.id: i for i in self.service.list_trash()}
        ids.sort(key=lambda t: order.get(items[t].kind, 3) if t in items else 9)
        problems = []

        def work():
            for tid in ids:
                try:
                    self.service.restore(tid)
                except RestoreConflictError as exc:
                    problems.append(str(exc))

        def done(_r):
            self.refresh()
            self.changed.emit()
            if problems:
                QMessageBox.warning(self, "Restore", "\n".join(dict.fromkeys(problems)))

        self.runner.submit(work, on_done=done)

    def purge(self) -> None:
        ids = self._selected_ids()
        if not ids:
            return
        n = len(ids)
        if not confirm(self, "Delete permanently",
                       f"Permanently delete {n} item(s)? They cannot be restored afterwards.",
                       "Delete permanently", destructive=True):
            return
        if not confirm(self, "Are you sure?", "This really cannot be undone.", "Yes, delete forever",
                       destructive=True):
            return
        for tid in ids:
            self.service.purge(tid)
        self.refresh()
        self.changed.emit()

    def delete_target(self, focus):
        """What ⌫ deletes: the selected items, for good (after asking twice, as the button does)."""
        if has_focus_in(self.tree, focus) and self._selected_ids():
            return self.purge
        return None

    def clear(self) -> None:
        self.tree.clear()
        self._update_buttons()
