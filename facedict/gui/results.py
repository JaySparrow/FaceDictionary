"""The top-5 result cards shared by the Search and History pages."""

from __future__ import annotations

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (QFrame, QHBoxLayout, QLabel, QPushButton, QScrollArea, QSizePolicy, QVBoxLayout,
                               QWidget)

from .. import config
from ..models import HistoryRecord
from ..vault import VaultError
from .common import (ClickableLabel, ElidedLabel, ScoreBar, bytes_to_pixmap, make_labels_selectable, muted,
                     placeholder_pixmap, rounded)

THUMB = 92


class ResultCard(QFrame):
    confirm_clicked = Signal(str)
    undo_clicked = Signal()
    open_clicked = Signal(str)

    def __init__(self, rank: int, entry_id: str, title: str, subtitle: str, score: float, low: float,
                 thumb: QPixmap, state: str, entry_exists: bool, parent=None):
        """state: 'open' (nothing confirmed yet), 'this' (confirmed as this entry), 'other'."""
        super().__init__(parent)
        self.setObjectName("card")
        self.entry_id = entry_id

        pic = ClickableLabel(tooltip="Open this entry")
        pic.setFixedSize(THUMB, THUMB)
        pic.setPixmap(rounded(thumb.scaled(QSize(THUMB, THUMB) * self.devicePixelRatioF(),
                                           Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                                           Qt.TransformationMode.SmoothTransformation), 10))
        pic.setScaledContents(True)

        badge = QLabel(str(rank))
        badge.setObjectName("muted")
        badge.setFixedWidth(14)
        self.name_label = ClickableLabel(title, "Open this entry")
        name = self.name_label
        f = name.font()
        f.setPointSize(17)
        name.setFont(f)
        for w in (pic, name):
            w.clicked.connect(lambda: self.open_clicked.emit(self.entry_id))
            w.setEnabled(entry_exists)
        top = QHBoxLayout()
        top.addWidget(badge)
        top.addWidget(name, 1)

        bar = ScoreBar()
        bar.set_score(score, low)
        bar.setFixedWidth(110)
        number = QLabel(f"{score:.3f}")
        number.setObjectName("muted")
        if score < low:
            number.setStyleSheet("color: #d4761e;")             # a weak match
        sub = ElidedLabel(subtitle)
        score_row = QHBoxLayout()
        score_row.setSpacing(8)
        score_row.addWidget(bar, 0, Qt.AlignmentFlag.AlignVCenter)
        score_row.addWidget(number)
        score_row.addWidget(sub, 1)

        buttons = QHBoxLayout()
        buttons.setSpacing(6)
        if state == "this":
            done = QLabel("✓ Added to album")
            done.setStyleSheet("color: #3a9d5d; font-weight: 600;")
            undo = QPushButton("Undo")
            undo.setToolTip("Remove the search photo from this entry's album again (it goes to the Trash)")
            undo.clicked.connect(self.undo_clicked.emit)
            buttons.addWidget(done)
            buttons.addWidget(undo)
        else:
            self.confirm_btn = QPushButton("✓ This is the person")
            self.confirm_btn.setEnabled(entry_exists and state == "open")
            self.confirm_btn.setToolTip("Confirm the match: a copy of the search photo is added to this entry's album")
            self.confirm_btn.clicked.connect(lambda: self.confirm_clicked.emit(self.entry_id))
            buttons.addWidget(self.confirm_btn)
        buttons.addStretch(1)

        info = QVBoxLayout()
        info.setSpacing(4)
        info.addLayout(top)
        info.addLayout(score_row)
        info.addLayout(buttons)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(10, 10, 10, 10)
        lay.setSpacing(14)
        lay.addWidget(pic, 0, Qt.AlignmentFlag.AlignTop)
        lay.addLayout(info, 1)

    def name_text(self) -> str:
        return self.name_label.text()

    def open_entry(self) -> None:
        self.open_clicked.emit(self.entry_id)


class ResultsPanel(QWidget):
    """Top-5 cards for one face of a history record. Emits `changed` after a confirmation or undo."""

    open_entry = Signal(str)
    changed = Signal(str)                # history id

    def __init__(self, service, runner, parent=None):
        super().__init__(parent)
        self.service = service
        self.runner = runner
        self.record: HistoryRecord | None = None
        self.face_index = 0
        self.caption = muted()
        self.caption.setVisible(False)
        self.banner = QLabel()
        self.banner.setObjectName("banner")
        self.banner.setWordWrap(True)
        self.banner.setVisible(False)
        self.empty = muted()
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.list = QVBoxLayout()
        self.list.setSpacing(8)
        holder = QWidget()
        hl = QVBoxLayout(holder)
        hl.setContentsMargins(0, 0, 4, 0)
        hl.addWidget(self.caption)
        hl.addWidget(self.banner)
        hl.addLayout(self.list)
        hl.addStretch(1)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setWidget(holder)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.empty)
        lay.addWidget(scroll, 1)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMinimumWidth(370)

    def clear(self, message: str = "") -> None:
        self.record = None
        self.face_index = 0
        self.caption.setVisible(False)
        self.banner.setVisible(False)
        while self.list.count():
            item = self.list.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self.empty.setText(message)
        self.empty.setVisible(True)

    def cards(self) -> list[ResultCard]:
        return [self.list.itemAt(i).widget() for i in range(self.list.count())
                if isinstance(self.list.itemAt(i).widget(), ResultCard)]

    def show_record(self, record: HistoryRecord, face_index: int | None = None) -> None:
        """Show the stored result of one face (default: the record's selected face)."""
        self.clear()
        self.record = record
        self.face_index = record.selected if face_index is None else face_index
        search = record.search(self.face_index)
        if record.face_count > 1:
            self.caption.setText(f"Matches for face {self.face_index + 1} of {record.face_count}")
            self.caption.setVisible(True)
        if search is None or not search.matches:
            self.empty.setText("Your library has no photos to compare with yet.")
            return
        self.empty.setVisible(False)
        spec = config.MODELS.get(record.model)
        low = spec.low_confidence if spec else 0.3
        if search.matches[0].score < low:
            self.banner.setText("Low confidence: even the best match is weak. The photo may be unclear, "
                                "or the person may not be in your library yet.")
            self.banner.setVisible(True)
        for m in search.matches:
            try:
                entry = self.service.get_entry(m.entry_id)
                exists, title = True, entry.name
                bits = []
                if entry.name != m.name_snapshot:
                    bits.append(f"was “{m.name_snapshot}” at search time")
                if entry.aliases:
                    bits.append("aka " + ", ".join(entry.aliases))
                if entry.tags:
                    bits.append(" · ".join(entry.tags))
                bits.append(f"{len(entry.photos)} photo(s)")
                subtitle = " — ".join(bits)
            except VaultError:
                exists, title, subtitle = False, m.name_snapshot, "This entry has been deleted"
            thumb = placeholder_pixmap(THUMB)
            if m.best_photo_id:
                try:
                    pm = bytes_to_pixmap(self.service.thumb_bytes(m.best_photo_id))
                    if not pm.isNull():
                        thumb = pm
                except Exception:
                    pass
            if search.confirmed is None:
                state = "open"
            else:
                state = "this" if search.confirmed.entry_id == m.entry_id else "other"
            c = ResultCard(m.rank, m.entry_id, title, subtitle, m.score, low, thumb, state, exists)
            c.open_clicked.connect(self.open_entry.emit)
            c.confirm_clicked.connect(self._confirm)
            c.undo_clicked.connect(self._undo)
            make_labels_selectable(c)
            self.list.addWidget(c)
        if search.confirmed and search.confirmed.entry_id not in {m.entry_id for m in search.matches}:
            self.banner.setText("This face was added to another entry's album.")
            self.banner.setVisible(True)

    def _confirm(self, entry_id: str) -> None:
        hid, face = self.record.id, self.face_index
        self._set_enabled(False)
        self.runner.submit(lambda: self.service.confirm(hid, entry_id, face),
                           on_done=lambda _r: self._reload(hid, face), on_error=self._error)

    def _undo(self) -> None:
        hid, face = self.record.id, self.face_index
        self._set_enabled(False)
        self.runner.submit(lambda: self.service.unconfirm(hid, face),
                           on_done=lambda _r: self._reload(hid, face), on_error=self._error)

    def _reload(self, history_id: str, face_index: int) -> None:
        if self.service.unlocked:
            if self.record is not None and self.record.id == history_id:
                # keep showing whichever face is selected now (the user may have switched meanwhile)
                self.show_record(self.service.get_history(history_id), self.face_index)
            self.changed.emit(history_id)

    def _error(self, exc: Exception) -> None:
        from .common import default_error_handler
        self._set_enabled(True)
        default_error_handler(exc)

    def _set_enabled(self, enabled: bool) -> None:
        for c in self.cards():
            c.setEnabled(enabled)
