"""Main window: lock screen <-> workspace (Search, History, Library, Trash), auto-lock."""

from __future__ import annotations

import time

from PySide6.QtCore import QEvent, QObject, QSettings, QSize, Qt, QTimer
from PySide6.QtGui import QAction, QGuiApplication, QKeySequence
from PySide6.QtWidgets import (QApplication, QDialog, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem,
                               QMainWindow, QPlainTextEdit, QProgressBar, QProgressDialog, QStackedWidget,
                               QVBoxLayout, QWidget)

from .. import config
from .common import (GAP, PAGE_MARGIN, Runner, default_error_handler, header_row, icon_button, line_icon,
                     make_labels_selectable)
from .history_page import HistoryPage
from .library_page import LibraryPage
from .lock_screen import LockScreen
from .questions_dialog import QuestionsDialog
from .search_page import SearchPage
from .settings_dialog import SettingsDialog
from .trash_page import TrashPage

PAGES = ["Search", "History", "Library", "Trash"]
ACTIVITY_EVENTS = {QEvent.Type.MouseButtonPress, QEvent.Type.MouseMove, QEvent.Type.KeyPress, QEvent.Type.Wheel,
                   QEvent.Type.HoverMove, QEvent.Type.TouchBegin}


class AutoLock(QObject):
    """Locks after N idle minutes, measured in wall-clock time so that sleep counts as idle."""

    def __init__(self, window: "MainWindow"):
        super().__init__(window)
        self.window = window
        self.last_activity = time.time()
        QApplication.instance().installEventFilter(self)
        self.timer = QTimer(self)
        self.timer.setInterval(5000)
        self.timer.timeout.connect(self.check)
        self.timer.start()
        QGuiApplication.instance().applicationStateChanged.connect(lambda _s: self.check())

    def eventFilter(self, obj, event) -> bool:
        if event.type() in ACTIVITY_EVENTS:
            self.last_activity = time.time()
        return False

    @property
    def limit_seconds(self) -> float:
        return float(self.window.service.settings.get("auto_lock_minutes", config.AUTO_LOCK_SECONDS // 60)) * 60

    def check(self) -> None:
        if self.window.service.unlocked and time.time() - self.last_activity > self.limit_seconds:
            self.window.lock(reason="Locked after inactivity.")


class MainWindow(QMainWindow):
    def __init__(self, service, parent=None):
        super().__init__(parent)
        self.service = service
        self.runner = Runner(self)
        self._stale: set[str] = set()
        self.setWindowTitle("FaceDictionary")
        self.resize(1240, 800)

        self.lock_screen = LockScreen(service, self.runner)
        self.lock_screen.unlocked.connect(self._on_unlocked)
        self.lock_screen.created.connect(self._setup_questions)

        self.search = SearchPage(service, self.runner)
        self.history = HistoryPage(service, self.runner)
        self.library = LibraryPage(service, self.runner)
        self.trash = TrashPage(service, self.runner)
        self.pages = {"Search": self.search, "History": self.history, "Library": self.library, "Trash": self.trash}

        self.nav = QListWidget()
        self.nav.setObjectName("nav")
        for page, icon in zip(PAGES, ("search", "history", "library", "trash")):
            self.nav.addItem(QListWidgetItem(line_icon(icon), page))
        self.nav.setIconSize(QSize(18, 18))
        self.nav.setFixedWidth(180)
        self.page_stack = QStackedWidget()
        for p in self.pages.values():
            self.page_stack.addWidget(p)
        self.nav.currentRowChanged.connect(self._switch_page)

        # no status bar: the rare messages (index being prepared, unreadable files) sit above the tools
        self.stats = QLabel()
        self.stats.setObjectName("muted")
        self.stats.setWordWrap(True)
        self.busy = QProgressBar()
        self.busy.setObjectName("score")                         # thin, like the similarity meters
        self.busy.setTextVisible(False)
        self.busy.setFixedHeight(4)
        self.busy.setVisible(False)
        self.runner.busy_changed.connect(self._busy)

        settings_btn = icon_button("settings", "Settings")
        settings_btn.clicked.connect(self.open_settings)
        lock_btn = icon_button("lock", "Lock now (⌘L)")
        lock_btn.clicked.connect(lambda: self.lock())
        tools = QHBoxLayout()
        tools.addWidget(settings_btn)
        tools.addWidget(lock_btn)
        tools.addStretch(1)
        side = QWidget()
        side.setObjectName("sidebar")
        side.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        sl = QVBoxLayout(side)
        sl.setContentsMargins(8, PAGE_MARGIN, 8, PAGE_MARGIN)          # headers line up with the pages
        sl.setSpacing(GAP)
        sl.addWidget(header_row(""))                             # the pages start below a header like this
        sl.addWidget(self.nav, 1)
        sl.addWidget(self.stats)
        sl.addWidget(self.busy)
        sl.addLayout(tools)
        workspace = QWidget()
        wl = QHBoxLayout(workspace)
        wl.setContentsMargins(0, 0, 0, 0)
        wl.setSpacing(0)
        wl.addWidget(side)
        wl.addWidget(self.page_stack, 1)

        self.stack = QStackedWidget()
        self.stack.addWidget(self.lock_screen)
        self.stack.addWidget(workspace)
        self.setCentralWidget(self.stack)

        # page signals
        self.search.open_entry.connect(self.open_entry)
        self.history.open_entry.connect(self.open_entry)
        self.search.searched.connect(lambda _h: self._changed(except_page="Search"))
        self.search.changed.connect(lambda h: self._changed(except_page="Search", history_id=h))
        self.history.changed.connect(lambda h: self._changed(except_page="History", history_id=h))
        self.library.changed.connect(lambda: self._changed(except_page="Library"))
        self.library.reveal.connect(lambda: self._goto(2))
        self.trash.changed.connect(lambda: self._changed(except_page="Trash"))

        self._actions()
        make_labels_selectable(self)                            # all text can be selected and copied
        self.autolock = AutoLock(self)
        s = QSettings()
        if s.value("geometry") is not None:
            self.restoreGeometry(s.value("geometry"))
        self._show_locked()

    # --- actions ----------------------------------------------------------------------------------

    def _actions(self) -> None:
        def add(text, shortcut, slot):
            a = QAction(text, self)
            a.setShortcut(QKeySequence(shortcut))
            a.triggered.connect(slot)
            self.addAction(a)
            return a

        add("Lock", "Ctrl+L", lambda: self.lock())
        add("Open photo", "Ctrl+O", self._open_shortcut)
        add("Paste photo", QKeySequence.StandardKey.Paste, self._paste_shortcut)
        add("Find entry", "Ctrl+F", self._find_shortcut)
        add("New entry", "Ctrl+N", self._new_entry_shortcut)
        for i in range(len(PAGES)):
            add(PAGES[i], f"Ctrl+{i + 1}", lambda _c=False, i=i: self._goto(i))
        self._edit_menu()

    def _edit_menu(self) -> None:
        """Edit › Undo / Redo (⌘Z / ⇧⌘Z) for every change made in the Library, step after step.
        Inside a text box that is open, the keys undo the typing instead, as everywhere on the Mac."""
        stack = self.library.undo_stack
        self.undo_action = QAction("Undo", self)
        self.undo_action.setShortcut(QKeySequence.StandardKey.Undo)
        self.undo_action.triggered.connect(self.library.undo)
        self.redo_action = QAction("Redo", self)
        self.redo_action.setShortcut(QKeySequence.StandardKey.Redo)
        self.redo_action.triggered.connect(self.library.redo)

        def sync(*_args) -> None:
            self.undo_action.setEnabled(stack.canUndo())
            self.undo_action.setText(f"Undo {stack.undoText()}" if stack.canUndo() else "Undo")
            self.redo_action.setEnabled(stack.canRedo())
            self.redo_action.setText(f"Redo {stack.redoText()}" if stack.canRedo() else "Redo")

        for signal in (stack.indexChanged, stack.canUndoChanged, stack.canRedoChanged, stack.undoTextChanged,
                       stack.redoTextChanged):
            signal.connect(sync)
        sync()
        menu = self.menuBar().addMenu("Edit")
        menu.addAction(self.undo_action)
        menu.addAction(self.redo_action)
        self.delete_action = QAction("Delete", self)
        self.delete_action.setShortcuts([QKeySequence(Qt.Key.Key_Backspace), QKeySequence(Qt.Key.Key_Delete)])
        self.delete_action.triggered.connect(self._delete_shortcut)
        self.addAction(self.delete_action)
        menu.addSeparator()
        menu.addAction(self.delete_action)
        menu.addSeparator()
        for action in self.library.add_actions.values():
            menu.addAction(action)
            self.addAction(action)

    def _workspace_active(self) -> bool:
        return self.service.unlocked and self.stack.currentIndex() == 1

    def _goto(self, index: int) -> None:
        if self._workspace_active():
            self.nav.setCurrentRow(index)

    def _open_shortcut(self) -> None:
        """⌘O: photos for the entry shown in the Library; anywhere else, a photo to search."""
        if not self._workspace_active():
            return
        if self.page_stack.currentWidget() is self.library and self.library.entry is not None:
            self.library.add_photos()
            return
        self._goto(0)
        self.search.open_photo()

    def _delete_shortcut(self) -> None:
        """⌫ / ⌦ on every page: delete what is selected in the list or album that has the keyboard
        focus, the same way its delete button does. Text boxes keep the keys for typing."""
        if not self._workspace_active():
            return
        page = self.page_stack.currentWidget()
        target = page.delete_target(QApplication.focusWidget()) if hasattr(page, "delete_target") else None
        if target is None:
            QApplication.beep()
            return
        target()

    def _paste_shortcut(self) -> None:
        focus = QApplication.focusWidget()
        if isinstance(focus, (QLineEdit, QPlainTextEdit)):
            focus.paste()
            return
        if self._workspace_active() and self.page_stack.currentWidget() is self.search:
            self.search.paste()

    def _find_shortcut(self) -> None:
        if self._workspace_active():
            self._goto(2)
            self.library.filter.setFocus()

    def _new_entry_shortcut(self) -> None:
        if self._workspace_active():
            self._goto(2)
            self.library.new_entry()

    # --- navigation -----------------------------------------------------------------------------------

    def _switch_page(self, row: int) -> None:
        if row < 0:
            return
        name = PAGES[row]
        self.page_stack.setCurrentWidget(self.pages[name])
        if name in self._stale:
            self._stale.discard(name)
            if name != "Search":
                self.pages[name].refresh()

    def _changed(self, except_page: str, history_id: str | None = None) -> None:
        current = PAGES[self.nav.currentRow()] if self.nav.currentRow() >= 0 else None
        for name in ("History", "Library", "Trash"):
            if name == except_page:
                continue
            if name == current:
                self.pages[name].refresh()
            else:
                self._stale.add(name)
        rec = self.search.results.record
        if history_id and except_page != "Search" and rec is not None and rec.id == history_id:
            try:
                self.search.results.show_record(self.service.get_history(history_id), self.search.results.face_index)
            except Exception:
                self.search.results.clear()
        self._update_status()

    def open_entry(self, entry_id: str) -> None:
        self.nav.setCurrentRow(2)
        self.library.refresh(entry_id)
        self.library.select_entry(entry_id)

    # --- lock / unlock ---------------------------------------------------------------------------------

    def _show_locked(self, reason: str = "") -> None:
        self.lock_screen.reset()
        if reason:
            self.lock_screen.error.setText(reason)
        self.stack.setCurrentIndex(0)
        self.setWindowTitle("FaceDictionary — Locked")
        self.stats.setText("")

    def _on_unlocked(self) -> None:
        self.autolock.last_activity = time.time()
        self.stack.setCurrentIndex(1)
        self.setWindowTitle(f"FaceDictionary — {self.service.vault_name}")
        self._stale = {"History", "Library", "Trash"}
        self.nav.setCurrentRow(0)
        self.search.clear()
        self._prepare_index()

    def _prepare_index(self) -> None:
        self.stats.setText("Preparing the face index…")

        def progress(done, total):
            self.stats.setText(f"Preparing the face index… {done}/{total}")

        self.runner.submit(lambda p, c: self.service.ensure_index(p, c), on_done=lambda _r: self._update_status(),
                           on_progress=progress, with_progress=True)

    def _setup_questions(self, password: str) -> None:
        """Right after a database is created, offer to set the security questions."""
        QTimer.singleShot(0, lambda: QuestionsDialog(self.service, password, self.runner, self,
                                                     first_time=True).exec())

    def lock(self, reason: str = "") -> None:
        if not self.service.unlocked:
            return
        try:
            self.library.commit_edits()                    # never lose what is being typed to an auto-lock
        except Exception:
            pass
        for w in QApplication.topLevelWidgets():         # close dialogs that may show personal data
            if isinstance(w, QDialog) and w.isVisible():
                w.done(QDialog.DialogCode.Rejected)
        self.runner.invalidate()
        self.runner.cancel_all_and_wait()
        self.service.lock()
        for page in self.pages.values():
            page.clear()
        self._stale = set()
        self._show_locked(reason)

    # --- misc ---------------------------------------------------------------------------------------------

    def _busy(self, busy: bool) -> None:
        self.busy.setRange(0, 0)
        self.busy.setVisible(busy)

    def _update_status(self) -> None:
        """The status bar stays empty unless something needs attention."""
        if not self.service.unlocked:
            return
        bad = len(self.service.vault.unreadable)
        self.stats.setText(f"⚠ {bad} file(s) could not be read – see Settings › Check database integrity"
                           if bad else "")

    def open_settings(self) -> None:
        if not self.service.unlocked:
            return
        dlg = SettingsDialog(self.service, self.runner, self)
        if dlg.exec() != QDialog.DialogCode.Accepted or not self.service.unlocked:
            return
        dlg.apply_simple_settings()
        if dlg.chosen_model != self.service.recognizer:
            progress = QProgressDialog("Re-computing the face index…", None, 0, 0, self)
            progress.setWindowModality(Qt.WindowModality.WindowModal)
            progress.setMinimumDuration(0)

            def step(done, total):
                progress.setMaximum(total)
                progress.setValue(done)

            def finished(_r):
                progress.close()
                self._update_status()

            def failed(exc):
                progress.close()
                default_error_handler(exc)

            model = dlg.chosen_model
            self.runner.submit(lambda p, c: self.service.set_recognizer(model, p, c), on_done=finished,
                               on_error=failed, on_progress=step, with_progress=True)

    def closeEvent(self, event) -> None:
        QSettings().setValue("geometry", self.saveGeometry())
        self.lock()
        self.runner.pool.waitForDone(5000)
        event.accept()
