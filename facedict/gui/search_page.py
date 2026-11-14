"""Search page: drop / open / paste a photo, see the top-5 entries."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QFrame, QSplitter, QVBoxLayout, QWidget

from ..imageio import is_supported
from .common import GAP, GUTTER, PAGE_MARGIN, header_row, icon_button, muted, open_images, qimage_to_png_bytes
from .faces_view import QueryView, render_photo
from .results import ResultsPanel


class DropZone(QFrame):
    dropped = Signal(bytes)
    clicked = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("dropzone")
        self.setAcceptDrops(True)
        self.view = QueryView()
        self.hint = QWidget()                     # the empty zone: drop, click, ⌘V or ⌘O
        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 14, 14, 14)
        lay.addWidget(self.hint, 1)
        lay.addWidget(self.view, 1)
        self.show_hint(True)

    def show_hint(self, on: bool) -> None:
        self.hint.setVisible(on)
        self.view.setVisible(not on)
        self.setProperty("filled", not on)                          # no background around a photo
        self.layout().setContentsMargins(*((14, 14, 14, 14) if on else (0, 0, 0, 0)))
        self.style().unpolish(self)
        self.style().polish(self)
        if on:
            self.setCursor(Qt.CursorShape.PointingHandCursor)     # the empty zone opens a file chooser
        else:
            self.unsetCursor()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and not self.view.image.has_image:
            self.clicked.emit()

    def _set_hover(self, on: bool) -> None:
        self.setProperty("hover", "true" if on else "false")
        self.style().unpolish(self)
        self.style().polish(self)

    def dragEnterEvent(self, event):
        md = event.mimeData()
        if md.hasImage() or any(is_supported(u.toLocalFile()) for u in md.urls() if u.isLocalFile()):
            event.acceptProposedAction()
            self._set_hover(True)

    def dragLeaveEvent(self, event):
        self._set_hover(False)

    def dropEvent(self, event):
        self._set_hover(False)
        data = mime_to_bytes(event.mimeData())
        if data:
            event.acceptProposedAction()
            self.dropped.emit(data)


def mime_to_bytes(md) -> bytes | None:
    for url in md.urls():
        if url.isLocalFile() and is_supported(url.toLocalFile()):
            try:
                return Path(url.toLocalFile()).read_bytes()
            except OSError:
                return None
    if md.hasImage():
        img = md.imageData()
        if img is not None and not img.isNull():
            return qimage_to_png_bytes(img)
    return None


class SearchPage(QWidget):
    open_entry = Signal(str)
    searched = Signal(str)               # history id
    changed = Signal(str)

    def __init__(self, service, runner, parent=None):
        super().__init__(parent)
        self.service = service
        self.runner = runner
        self._from_clipboard = False

        self.drop = DropZone()
        self.drop.dropped.connect(lambda data: self.search(data))
        self.drop.clicked.connect(self.open_photo)
        self.drop.view.face_selected.connect(self.select_face)
        self.status = muted()

        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        ll.setSpacing(GAP)
        self.open_btn = icon_button("plus", "Open a photo (⌘O)")      # also once a photo is shown
        self.open_btn.clicked.connect(self.open_photo)
        ll.addWidget(header_row("Search by face", self.open_btn))
        ll.addWidget(self.drop, 1)

        self.results = ResultsPanel(service, runner)
        self.results.open_entry.connect(self.open_entry.emit)
        self.results.changed.connect(self.changed.emit)
        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.setSpacing(GAP)
        rl.addWidget(header_row("Best matches"))
        rl.addWidget(self.results, 1)

        split = QSplitter()
        split.setHandleWidth(GUTTER)
        split.addWidget(left)
        split.addWidget(right)
        split.setStretchFactor(0, 6)
        split.setStretchFactor(1, 5)
        split.setChildrenCollapsible(False)
        split.setSizes([740, 420])                       # the photo gets the larger share
        lay = QVBoxLayout(self)
        lay.setContentsMargins(PAGE_MARGIN, PAGE_MARGIN, PAGE_MARGIN, PAGE_MARGIN)
        lay.setSpacing(GAP)
        lay.addWidget(split, 1)
        lay.addWidget(self.status)

    # --- input ---------------------------------------------------------------------------

    def open_photo(self) -> None:
        paths = open_images(self, "Choose a photo to search", multiple=False, kind="search")
        if paths:
            try:
                self.search(Path(paths[0]).read_bytes())
            except OSError as exc:
                self.status.setText(f"Cannot read the file: {exc}")

    def paste(self) -> None:
        data = mime_to_bytes(QGuiApplication.clipboard().mimeData())
        if data:
            self._from_clipboard = True
            self.search(data)
        else:
            self.status.setText("The clipboard has no image.")

    # --- search ----------------------------------------------------------------------------

    def search(self, data: bytes) -> None:
        if not self.service.unlocked:
            return
        self.status.setText("Searching…")
        self.results.clear("Searching…")
        from_clipboard, self._from_clipboard = self._from_clipboard, False

        def work():
            res = self.service.query(data)
            rendered = render_photo(res.image, res.faces)
            res.image = None                 # drop the full-size decode as early as possible
            return res, rendered

        def done(out):
            res, rendered = out
            self.drop.show_hint(False)
            self.drop.view.show_query(rendered, res.faces, res.selected, f"Search took {res.elapsed_ms:.0f} ms.")
            self.status.setText("")
            if res.history is None:
                self.results.clear("No face to search for.")
            else:
                self.results.show_record(res.history, res.selected)
                self.searched.emit(res.history.id)
            if from_clipboard and self.service.settings.get("clear_clipboard_after_search"):
                QGuiApplication.clipboard().clear()

        def failed(exc):
            self.status.setText("")
            self.results.clear(f"Could not search: {exc}")

        self.runner.submit(work, on_done=done, on_error=failed)

    def select_face(self, index: int) -> None:
        """Show the stored matches of another face; remember the choice for the History page."""
        rec = self.results.record
        if rec is None or not 0 <= index < rec.face_count:
            return
        self.drop.view.select(index)
        self.results.show_record(rec, index)
        hid = rec.id
        self.runner.submit(lambda: self.service.set_selected(hid, index),
                           on_done=lambda _r: self.changed.emit(hid))

    def clear(self) -> None:
        self.drop.view.clear()
        self.drop.show_hint(True)
        self.results.clear()
        self.status.setText("")
