"""History page: every past search with its stored top-5 result (shown without recomputing)."""

from __future__ import annotations

from datetime import datetime

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (QHBoxLayout, QListWidget, QListWidgetItem, QPushButton, QSplitter, QVBoxLayout,
                               QWidget)

from .. import config
from .common import (GAP, GUTTER, PAGE_MARGIN, add_copy_action, bytes_to_pixmap, confirm, has_focus_in,
                     header_row, heading, icon_button, muted, placeholder_pixmap)
from .faces_view import QueryView, render_photo
from .results import ResultsPanel

ROLE_ID = Qt.ItemDataRole.UserRole


def fmt_time(iso: str) -> str:
    try:
        return datetime.fromisoformat(iso).strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return iso


class HistoryPage(QWidget):
    open_entry = Signal(str)
    changed = Signal(str)

    def __init__(self, service, runner, parent=None):
        super().__init__(parent)
        self.service = service
        self.runner = runner

        self.count = muted()
        self.list = QListWidget()
        self.list.setIconSize(QSize(52, 52))
        self.list.setSpacing(2)
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.list.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.list.setMinimumWidth(260)
        self.list.currentItemChanged.connect(lambda cur, _prev: self._select(cur))
        add_copy_action(self.list)
        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        ll.setSpacing(GAP)
        ll.addWidget(header_row("History", self.count))
        ll.addWidget(self.list, 1)

        self.query = QueryView()
        self.query.face_selected.connect(self.select_face)
        self.results = ResultsPanel(service, runner)
        self.results.open_entry.connect(self.open_entry.emit)
        self.results.changed.connect(self._record_changed)
        self.rerun_btn = QPushButton("Search again")
        self.rerun_btn.setToolTip("Search this photo again with the library as it is now")
        self.rerun_btn.clicked.connect(self._rerun)
        self.delete_btn = icon_button("trash", "Move this search record to the Trash")
        self.delete_btn.clicked.connect(self._delete)
        detail = QWidget()
        dl = QHBoxLayout(detail)
        dl.setContentsMargins(0, 0, 0, 0)
        dl.addWidget(self.query, 5)
        dl.addWidget(self.results, 4)
        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.setSpacing(GAP)
        self.title = heading("", 15)
        self.header = header_row("", self.rerun_btn, self.delete_btn)
        self.header.layout().insertWidget(0, self.title)
        rl.addWidget(self.header)
        rl.addWidget(detail, 1)

        split = QSplitter()
        split.setHandleWidth(GUTTER)
        split.addWidget(left)
        split.addWidget(right)
        split.setStretchFactor(0, 2)
        split.setStretchFactor(1, 7)
        split.setChildrenCollapsible(False)
        split.setSizes([280, 980])
        lay = QVBoxLayout(self)
        lay.setContentsMargins(PAGE_MARGIN, PAGE_MARGIN, PAGE_MARGIN, PAGE_MARGIN)
        lay.addWidget(split)
        self._show_detail(False)

    def _show_detail(self, on: bool) -> None:
        for w in (self.query, self.results, self.rerun_btn, self.delete_btn):
            w.setVisible(on)
        if not on:
            self.title.setText("")

    def refresh(self, select_id: str | None = None) -> None:
        if not self.service.unlocked:
            return
        current = select_id or (self.list.currentItem().data(ROLE_ID) if self.list.currentItem() else None)
        self.list.blockSignals(True)
        self.list.clear()
        records = self.service.list_history()
        for rec in records:
            item = QListWidgetItem(self._item_text(rec))
            try:
                pm = bytes_to_pixmap(self.service.thumb_bytes(rec.photo_id, rec.face))
            except Exception:
                pm = placeholder_pixmap(52)
            item.setIcon(QIcon(pm))
            item.setData(ROLE_ID, rec.id)
            self.list.addItem(item)
            if rec.id == current:
                self.list.setCurrentItem(item)
        self.list.blockSignals(False)
        self.count.setText(f"{len(records)} search(es)" if records else "No searches yet.")
        self._select(self.list.currentItem())

    def _item_text(self, rec) -> str:
        if rec.matches:
            top = rec.matches[0]
            try:
                top_name = self.service.get_entry(top.entry_id).name
            except Exception:
                top_name = top.name_snapshot
            line2 = f"{top_name} · {top.score:.2f}"
        else:
            line2 = "no matches"
        if rec.face_count > 1:
            line2 += f"  ({rec.face_count} faces)"
        if rec.any_confirmed:
            line2 += "  ✓"
        return f"{fmt_time(rec.time)}\n{line2}"

    def _select(self, item: QListWidgetItem | None) -> None:
        if item is None or not self.service.unlocked:
            self.query.clear()
            self.results.clear()
            self._show_detail(False)
            return
        rec = self.service.get_history(item.data(ROLE_ID))
        self._show_detail(True)
        spec = config.MODELS.get(rec.model)
        self.title.setText(f"Search from {fmt_time(rec.time)}")
        self.results.show_record(rec, rec.selected)    # stored result - nothing is recomputed
        self.query.clear()
        extra = f"Model: {spec.label if spec else rec.model}."
        faces = [s.face for s in rec.searches]

        def work():
            return render_photo(self.service.photo_image(rec.photo_id), faces)

        def done(rendered):
            cur = self.list.currentItem()
            if cur is not None and cur.data(ROLE_ID) == rec.id:
                self.query.show_query(rendered, faces, self.results.face_index, extra)

        self.runner.submit(work, on_done=done)

    def select_face(self, index: int) -> None:
        """Switch to another face of the shown search (its result is stored, nothing is recomputed)."""
        rec = self.results.record
        if rec is None or not 0 <= index < rec.face_count:
            return
        self.query.select(index)
        self.results.show_record(rec, index)
        hid = rec.id

        def done(updated):
            item = self.list.currentItem()
            if item is not None and item.data(ROLE_ID) == hid:
                item.setText(self._item_text(updated))
            self.changed.emit(hid)

        self.runner.submit(lambda: self.service.set_selected(hid, index), on_done=done)

    def _record_changed(self, history_id: str) -> None:
        self.refresh(history_id)
        self.changed.emit(history_id)

    def _current_id(self) -> str | None:
        item = self.list.currentItem()
        return item.data(ROLE_ID) if item else None

    def _rerun(self) -> None:
        hid = self._current_id()
        if hid:
            self.rerun_btn.setEnabled(False)

            def done(res):
                self.rerun_btn.setEnabled(True)
                new_id = res.history.id if res.history else None
                self.refresh(new_id)
                self.changed.emit(new_id or "")

            def failed(exc):
                self.rerun_btn.setEnabled(True)
                from .common import default_error_handler
                default_error_handler(exc)

            self.runner.submit(lambda: self.service.rerun(hid), on_done=done, on_error=failed)

    def _delete(self) -> None:
        hid = self._current_id()
        if hid and confirm(self, "Delete record", "Move this search record and its photo to the Trash?\n"
                           "Photos already added to an entry's album are not affected.", "Move to Trash"):
            self.service.delete_history(hid)
            self.refresh()
            self.changed.emit(hid)

    def delete_target(self, focus):
        """What ⌫ deletes: the selected search record."""
        if has_focus_in(self.list, focus) and self._current_id():
            return self._delete
        return None

    def clear(self) -> None:
        self.list.clear()
        self.query.clear()
        self.results.clear()
        self.count.setText("")
        self._show_detail(False)
