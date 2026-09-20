"""Unlock screen: choose a database, create or unlock it, or recover its password."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QFont, QFontMetrics
from PySide6.QtWidgets import (QCheckBox, QDialog, QDialogButtonBox, QFormLayout, QFrame, QHBoxLayout, QInputDialog,
                               QLabel, QLineEdit, QMenu, QPushButton, QStackedWidget, QVBoxLayout, QWidget)

from .. import databases
from ..crypto import WrongPasswordError
from ..vault import VaultInfo
from .common import card, choose_folder, confirm, heading, muted
from .questions_dialog import RecoveryQuestionsForm

MIN_PASSWORD = 8


def _password_field(placeholder: str) -> QLineEdit:
    f = QLineEdit()
    f.setEchoMode(QLineEdit.EchoMode.Password)
    f.setPlaceholderText(placeholder)
    f.setMinimumWidth(280)
    return f


class DatabaseBar(QFrame):
    """Shows which database is selected and lets the user switch to another one."""

    picked = Signal(object)                     # Path

    def __init__(self, service, parent=None):
        super().__init__(parent)
        self.service = service
        self.setObjectName("card")
        self.path: Path | None = None
        self.name = QLabel()
        f = self.name.font()
        f.setBold(True)
        self.name.setFont(f)
        self.where = muted()
        self.where.setWordWrap(False)
        self.button = QPushButton("Open another…")
        self.menu = QMenu(self)
        self.menu.aboutToShow.connect(self._build_menu)
        self.button.setMenu(self.menu)
        left = QVBoxLayout()
        left.setSpacing(1)
        left.addWidget(self.name)
        left.addWidget(self.where)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 8, 12, 8)
        lay.addLayout(left, 1)
        lay.addWidget(self.button)

    def set_database(self, path: Path) -> None:
        self.path = Path(path)
        self.name.setText(databases.display_name(self.path))
        self._elide()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._elide()

    def _elide(self) -> None:
        if self.path is None:
            return
        text = str(self.path).replace(str(Path.home()), "~")
        width = max(160, self.width() - self.button.width() - 50)
        self.where.setText(QFontMetrics(self.where.font()).elidedText(text, Qt.TextElideMode.ElideMiddle, width))
        self.where.setToolTip(str(self.path))

    def _build_menu(self) -> None:
        self.menu.clear()
        for ref in self.service.recent_databases():
            if ref.path == self.path:
                continue
            action = self.menu.addAction(f"{ref.name}   ({str(ref.path).replace(str(Path.home()), '~')})")
            action.triggered.connect(lambda _c=False, p=ref.path: self.picked.emit(p))
        if not self.menu.isEmpty():
            self.menu.addSeparator()
        self.menu.addAction("Choose a database folder…").triggered.connect(self._choose_existing)
        self.menu.addAction("Create a new database…").triggered.connect(self._create_new)
        self.menu.addSeparator()
        forget = self.menu.addAction("Forget this database (keeps the files)")
        forget.triggered.connect(self._forget)
        clear = self.menu.addAction("Clear the list of databases")
        clear.triggered.connect(self._clear)
        clear.setEnabled(bool(self.service.recent_databases()))

    def _choose_existing(self) -> None:
        folder = choose_folder(self, "Choose a database folder")
        if folder:
            self.picked.emit(Path(folder))

    def _create_new(self) -> None:
        parent = choose_folder(self, "Where should the new database be kept?")
        if not parent:
            return
        name, ok = QInputDialog.getText(self, "New database", "Name of the new database:")
        name = name.strip()
        if not ok or not name:
            return
        safe = "".join(ch for ch in name if ch not in '/\\:*?"<>|').strip() or "database"
        self.picked.emit(Path(parent) / f"{safe}.noindex")

    def _forget(self) -> None:
        if self.path is not None:
            databases.forget(self.path)
        self.picked.emit(self.path)

    def _clear(self) -> None:
        if confirm(self, "Clear the list", "Forget the list of databases you have opened? "
                   "No database is deleted; you can open them again by choosing their folder.", "Clear list"):
            databases.clear()


class LockScreen(QWidget):
    unlocked = Signal()
    created = Signal(str)                      # the password of the new database

    def __init__(self, service, runner, parent=None):
        super().__init__(parent)
        self.service = service
        self.runner = runner
        self.info: VaultInfo | None = None

        title = heading("FaceDictionary", 26)
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        subtitle = muted("Private face lookup. Everything stays on this Mac, encrypted.")
        subtitle.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.bar = DatabaseBar(service)
        self.bar.picked.connect(self.open_database)

        self.pages = QStackedWidget()
        self.pages.addWidget(self._build_create())      # 0
        self.pages.addWidget(self._build_unlock())      # 1
        self.recover_page = QWidget()
        self.recover_layout = QVBoxLayout(self.recover_page)
        self.recover_layout.setContentsMargins(0, 0, 0, 0)
        self.pages.addWidget(self.recover_page)         # 2
        self.pages.addWidget(self._build_problem())     # 3

        self.error = QLabel()
        self.error.setStyleSheet("color: #c9483c;")
        self.error.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.error.setWordWrap(True)

        box = card()
        inner = QVBoxLayout(box)
        inner.setContentsMargins(28, 24, 28, 24)
        inner.setSpacing(12)
        inner.addWidget(title)
        inner.addWidget(subtitle)
        inner.addSpacing(6)
        inner.addWidget(self.bar)
        inner.addWidget(self.pages)
        inner.addWidget(self.error)
        box.setMaximumWidth(560)

        outer = QVBoxLayout(self)
        outer.addStretch(1)
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(box)
        row.addStretch(1)
        outer.addLayout(row)
        outer.addStretch(2)

    # --- pages -------------------------------------------------------------------------

    def _build_create(self) -> QWidget:
        w = QWidget()
        self.new_pw = _password_field(f"At least {MIN_PASSWORD} characters")
        self.new_pw2 = _password_field("Repeat the password")
        self.create_btn = QPushButton("Create encrypted database")
        self.create_btn.setObjectName("primary")
        self.create_btn.setDefault(True)
        self.create_btn.clicked.connect(self._create)
        self.new_pw2.returnPressed.connect(self._create)
        form = QFormLayout()
        form.addRow("Password", self.new_pw)
        form.addRow("Confirm", self.new_pw2)
        self.create_hint = muted("This folder is empty. Choose a password for the new database – "
                                 "you will pick security questions next, in case you ever forget it.")
        self.create_hint.setWordWrap(True)
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.create_hint)
        lay.addLayout(form)
        lay.addWidget(self.create_btn)
        return w

    def _build_unlock(self) -> QWidget:
        w = QWidget()
        self.pw = _password_field("Password of this database")
        self.pw.returnPressed.connect(self._unlock)
        self.unlock_btn = QPushButton("Unlock")
        self.unlock_btn.setObjectName("primary")
        self.unlock_btn.setDefault(True)
        self.unlock_btn.clicked.connect(self._unlock)
        self.forgot_btn = QPushButton("Forgot the password?")
        self.forgot_btn.setFlat(True)
        self.forgot_btn.clicked.connect(self._show_recover)
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.pw)
        lay.addWidget(self.unlock_btn)
        lay.addWidget(self.forgot_btn, alignment=Qt.AlignmentFlag.AlignCenter)
        return w

    def _build_problem(self) -> QWidget:
        w = QWidget()
        self.problem = muted()
        self.problem.setWordWrap(True)
        self.problem.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.problem)
        lay.addWidget(muted("Use “Open another…” above to pick a different folder."),
                      alignment=Qt.AlignmentFlag.AlignCenter)
        return w

    # --- database selection ----------------------------------------------------------------

    def open_database(self, path: Path) -> None:
        try:
            self.service.use_database(Path(path))
        except Exception as exc:
            self.error.setText(str(exc))
            return
        self.reset()

    def reset(self) -> None:
        """Re-read the selected database and show the page that fits it."""
        for f in (self.new_pw, self.new_pw2, self.pw):
            f.clear()
        self.error.clear()
        self._set_busy(False)
        self.bar.set_database(self.service.vault_root)
        self.info = self.service.inspect()
        if self.info.error:
            self.problem.setText(self.info.error)
            self.pages.setCurrentIndex(3)
            return
        if self.info.usable:
            self.pages.setCurrentIndex(1)
            self.forgot_btn.setVisible(bool(self.info.recovery_methods))
            QTimer.singleShot(0, self.pw.setFocus)
        elif self.info.can_create:
            self.pages.setCurrentIndex(0)
            QTimer.singleShot(0, self.new_pw.setFocus)
        else:
            self.problem.setText("This folder cannot be used as a database.")
            self.pages.setCurrentIndex(3)

    # --- actions ------------------------------------------------------------------------------

    def _set_busy(self, busy: bool) -> None:
        for b in (self.create_btn, self.unlock_btn):
            b.setEnabled(not busy)
        if hasattr(self, "recover_btn"):
            self.recover_btn.setEnabled(not busy)

    def _check_new_password(self, a: str, b: str) -> bool:
        if len(a) < MIN_PASSWORD:
            self.error.setText(f"The password must have at least {MIN_PASSWORD} characters.")
            return False
        if a != b:
            self.error.setText("The two passwords do not match.")
            return False
        return True

    def _create(self) -> None:
        a, b = self.new_pw.text(), self.new_pw2.text()
        if not self._check_new_password(a, b):
            return
        self._set_busy(True)
        self.error.clear()
        self.runner.submit(lambda: self.service.create_vault(a), on_done=lambda _r: self._created(a),
                           on_error=self._failed)

    def _created(self, password: str) -> None:
        self.new_pw.clear()
        self.new_pw2.clear()
        self._set_busy(False)
        self.created.emit(password)
        self.unlocked.emit()

    def _unlock(self) -> None:
        pw = self.pw.text()
        if not pw:
            return
        self._set_busy(True)
        self.error.clear()
        self.runner.submit(lambda: self.service.unlock(pw), on_done=lambda _r: self._opened(), on_error=self._failed)

    def _opened(self) -> None:
        for f in (self.pw, self.new_pw, self.new_pw2):
            f.clear()
        self._set_busy(False)
        self.unlocked.emit()

    def _failed(self, exc: Exception) -> None:
        self.pw.clear()
        self.error.setText(str(exc) if isinstance(exc, WrongPasswordError) else f"Could not open the database: {exc}")
        QTimer.singleShot(800, lambda: self._set_busy(False))      # slow down guessing a little

    # --- password recovery ------------------------------------------------------------------------

    def _show_recover(self, force_code: bool = False) -> None:
        """Build the recovery page for whatever this database offers."""
        info = self.info
        use_questions = bool(info.questions) and not force_code
        while self.recover_layout.count():
            item = self.recover_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
            elif item.layout():
                while item.layout().count():
                    sub = item.layout().takeAt(0)
                    if sub.widget():
                        sub.widget().deleteLater()
        self.questions_form = None
        self.code_field = None
        self.error.clear()

        self.recover_layout.addWidget(heading("Reset the password", 15))
        if use_questions:
            self.questions_form = RecoveryQuestionsForm(info.questions, info.required)
            self.recover_layout.addLayout(self.questions_form)
        elif info.has_code:
            self.recover_layout.addWidget(muted("Enter the recovery code you wrote down when the database "
                                                "was created."))
            self.code_field = QLineEdit()
            self.code_field.setPlaceholderText("XXXX-XXXX-XXXX-XXXX-XXXX-XXXX-XXXX-XXXX")
            self.code_field.setFont(QFont("Menlo"))
            self.recover_layout.addWidget(self.code_field)
        self.rec_pw = _password_field(f"New password, at least {MIN_PASSWORD} characters")
        self.rec_pw2 = _password_field("Repeat the new password")
        form = QFormLayout()
        form.addRow("New password", self.rec_pw)
        form.addRow("Confirm", self.rec_pw2)
        self.recover_layout.addLayout(form)
        self.recover_btn = QPushButton("Reset password and unlock")
        self.recover_btn.setObjectName("primary")
        self.recover_btn.clicked.connect(self._recover)
        self.recover_layout.addWidget(self.recover_btn)
        if info.has_code and info.questions:
            swap = QPushButton("Use the recovery code instead" if use_questions else "Use the security questions")
            swap.setFlat(True)
            swap.clicked.connect(lambda: self._show_recover(force_code=use_questions))
            self.recover_layout.addWidget(swap, alignment=Qt.AlignmentFlag.AlignCenter)
        back = QPushButton("Back")
        back.setFlat(True)
        back.clicked.connect(self.reset)
        self.recover_layout.addWidget(back, alignment=Qt.AlignmentFlag.AlignCenter)
        self.pages.setCurrentIndex(2)

    def _recover(self) -> None:
        a, b = self.rec_pw.text(), self.rec_pw2.text()
        if not self._check_new_password(a, b):
            return
        if self.questions_form is not None:
            answers = self.questions_form.answers()
            if not answers:
                self.error.setText("Please answer the questions you remember.")
                return
            job = lambda: self.service.recover_with_answers(answers, a)          # noqa: E731
        else:
            code = self.code_field.text().strip() if self.code_field else ""
            if not code:
                self.error.setText("Please enter the recovery code.")
                return
            job = lambda: self.service.recover(code, a)                          # noqa: E731
        self._set_busy(True)
        self.error.clear()
        self.runner.submit(job, on_done=lambda _r: self._opened(), on_error=self._failed)


class RecoveryCodeDialog(QDialog):
    def __init__(self, code: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Your recovery code")
        self.setModal(True)
        text = QLabel(code)
        f = QFont("Menlo")
        f.setPointSize(17)
        text.setFont(f)
        text.setAlignment(Qt.AlignmentFlag.AlignCenter)
        text.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
        text.setStyleSheet("padding: 14px; border: 1px solid palette(mid); border-radius: 8px;")
        info = muted("Write this code on paper and keep it somewhere safe, offline. It can reset this "
                     "database's password even if you forget the answers to your security questions. "
                     "It will not be shown again.")
        self.ack = QCheckBox("I have written the recovery code down")
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok)
        self.ok = buttons.button(QDialogButtonBox.StandardButton.Ok)
        self.ok.setEnabled(False)
        self.ack.toggled.connect(self.ok.setEnabled)
        buttons.accepted.connect(self.accept)
        lay = QVBoxLayout(self)
        lay.setSpacing(12)
        lay.addWidget(heading("Recovery code"))
        lay.addWidget(info)
        lay.addWidget(text)
        lay.addWidget(self.ack)
        lay.addWidget(buttons)

    def reject(self) -> None:          # Esc must not skip the acknowledgement
        if self.ack.isChecked():
            super().reject()
