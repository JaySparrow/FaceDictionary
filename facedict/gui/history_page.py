"""History page: every past search with its stored top-5 result (shown without recomputing)."""

from __future__ import annotations

from datetime import datetime

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (QAbstractItemView, QHBoxLayout, QListWidget, QListWidgetItem, QPushButton, QSplitter,
                               QVBoxLayout, QWidget)

from .. import config
from .common import (GAP, GUTTER, PAGE_MARGIN, TwoLineDelegate, add_copy_action, add_menu_action, bytes_to_pixmap,
                     confirm, default_error_handler, has_focus_in, header_row, heading, icon_button, muted,
                     placeholder_pixmap, plural, select_rows, selected_items)
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
        self._checked: set[str] = set()           # records whose thumbnail was checked this session

        self.count = muted()
        self.list = QListWidget()
        self.list.setIconSize(QSize(56, 56))
        self.list.setItemDelegate(TwoLineDelegate(56, round_picture=False, parent=self.list))
        self.list.setSpacing(2)
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.list.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.list.setMinimumWidth(260)
        self.list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.list.itemSelectionChanged.connect(self._selection_changed)
        add_copy_action(self.list)
        self.trash_action = add_menu_action(self.list, self._delete, separator=True)
        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        ll.setSpacing(GAP)
        ll.addWidget(header_row("History", self.count))
        ll.addWidget(self.list, 1)

        self.query = QueryView()
        self.query.image.align_top = True
        self.query.info.hide()
        self.query.face_selected.connect(self.select_face)
        self.results = ResultsPanel(service, runner)
        self.results.open_entry.connect(self.open_entry.emit)
        self.results.changed.connect(self._record_changed)
        self.rerun_btn = QPushButton("Search again")
        self.rerun_btn.setToolTip("Search this photo again with the library as it is now")
        self.rerun_btn.clicked.connect(self._rerun)
        self.delete_btn = icon_button("trash", "Move this search record to the Trash")
        self.delete_btn.clicked.connect(self._delete)
        detail = self.detail = QWidget()
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
        self.several = muted()                     # in place of the detail while several are selected
        self.several.setAlignment(Qt.AlignmentFlag.AlignCenter)
        rl.addWidget(self.header)
        rl.addWidget(detail, 1)
        rl.addWidget(self.several, 1)

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

    def _show_detail(self, on: bool, several: int = 0) -> None:
        for w in (self.query, self.results, self.rerun_btn, self.delete_btn):
            w.setVisible(on)
        self.detail.setVisible(not several)
        self.several.setVisible(bool(several))
        self.several.setText(f"{several} searches selected" if several else "")
        if not on:
            self.title.setText("")

    def refresh(self, select_id: str | None = None) -> None:
        if not self.service.unlocked:
            return
        keep = [select_id] if select_id else self._selected_ids()
        current = self.list.currentItem().data(ROLE_ID) if self.list.currentItem() else None
        self.list.blockSignals(True)
        self.list.clear()
        records = self.service.list_history()
        for rec in records:
            item = QListWidgetItem(self._item_text(rec))
            item.setData(ROLE_ID, rec.id)
            self._set_icon(item, rec)
            self.list.addItem(item)
        rows = [i for i, rec in enumerate(records) if rec.id in keep]
        select_rows(self.list, rows, next((r for r in rows if records[r].id == current), rows[0]) if rows else None)
        self.list.blockSignals(False)
        self.count.setText(f"{len(records)} search(es)" if records else "No searches yet.")
        self._selection_changed()
        self._fix_thumbs(records)

    def _selected_ids(self) -> list[str]:
        return [i.data(ROLE_ID) for i in selected_items(self.list)]

    def _selection_changed(self) -> None:
        items = selected_items(self.list)
        n = len(items)
        self.trash_action.setText(f"Move {plural(n, 'Record')} to Trash")
        self.trash_action.setEnabled(n > 0)
        if n > 1:
            self.query.clear()
            self.results.clear()
            self._show_detail(False, several=n)
        else:
            self._select(items[0] if items else None)

    def _set_icon(self, item: QListWidgetItem, rec) -> None:
        try:
            pm = bytes_to_pixmap(self.service.thumb_bytes(rec.photo_id, rec.face))
        except Exception:
            pm = placeholder_pixmap(52)
        item.setIcon(QIcon(pm))

    def _item(self, history_id: str) -> QListWidgetItem | None:
        return next((self.list.item(i) for i in range(self.list.count())
                     if self.list.item(i).data(ROLE_ID) == history_id), None)

    def _fix_thumbs(self, records) -> None:
        """Older versions kept the thumbnail of the face shown first (the largest), even after another
        face was picked. Make those again, once a session, in the background."""
        stale = [r.id for r in records if r.id not in self._checked and r.face_count > 1
                 and r.face is not max((s.face for s in r.searches), key=lambda f: f.area)]
        self._checked.update(r.id for r in records)
        if not stale:
            return

        def done(ids):
            for hid in ids:
                item = self._item(hid)
                if item is not None:
                    self._set_icon(item, self.service.get_history(hid))

        self.runner.submit(lambda: self.service.remake_history_thumbs(stale), on_done=done)

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
            if self._current_id() == rec.id:
                self.query.show_query(rendered, faces, self.results.face_index, extra)

        def failed(exc):
            if self._current_id() == rec.id:          # not when it was deleted meanwhile
                default_error_handler(exc)

        self.runner.submit(work, on_done=done, on_error=failed)

    def select_face(self, index: int) -> None:
        """Switch to another face of the shown search (its result is stored, nothing is recomputed)."""
        rec = self.results.record
        if rec is None or not 0 <= index < rec.face_count:
            return
        self.query.select(index)
        self.results.show_record(rec, index)
        hid = rec.id

        def done(updated):
            item = self._item(hid)
            if item is not None:
                item.setText(self._item_text(updated))
                self._set_icon(item, updated)              # the thumbnail shows the face picked
            self.changed.emit(hid)

        self.runner.submit(lambda: self.service.set_selected(hid, index), on_done=done)

    def _record_changed(self, history_id: str) -> None:
        self.refresh(history_id)
        self.changed.emit(history_id)

    def _current_id(self) -> str | None:
        """The record shown (None while several are selected)."""
        ids = self._selected_ids()
        return ids[0] if len(ids) == 1 else None

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
                default_error_handler(exc)

            self.runner.submit(lambda: self.service.rerun(hid), on_done=done, on_error=failed)

    def _delete(self) -> None:
        """Move the selected search records to the Trash."""
        ids = self._selected_ids()
        if not ids:
            return
        text = ("Move this search record and its photo to the Trash?" if len(ids) == 1 else
                f"Move these {len(ids)} search records and their photos to the Trash?")
        if confirm(self, "Delete record" if len(ids) == 1 else "Delete records",
                   text + "\nPhotos already added to an entry's album are not affected.", "Move to Trash"):
            for hid in ids:
                self.service.delete_history(hid)
            self.refresh()
            for hid in ids:
                self.changed.emit(hid)

    def delete_target(self, focus):
        """What ⌫ deletes: the selected search records."""
        if has_focus_in(self.list, focus) and self._selected_ids():
            return self._delete
        return None

    def clear(self) -> None:
        self._checked.clear()
        self.list.clear()
        self.query.clear()
        self.results.clear()
        self.count.setText("")
        self._show_detail(False)
