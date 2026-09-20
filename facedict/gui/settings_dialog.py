"""Settings: recognition model, auto-lock, clipboard, password and recovery code."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout,
                               QInputDialog, QLabel, QLineEdit, QMessageBox, QPushButton, QSpinBox, QVBoxLayout)

from .. import config
from ..crypto import WrongPasswordError
from .common import heading, muted
from .lock_screen import MIN_PASSWORD, RecoveryCodeDialog
from .questions_dialog import QuestionsDialog


def ask_password(parent, title: str, label: str) -> str | None:
    text, ok = QInputDialog.getText(parent, title, label, QLineEdit.EchoMode.Password)
    return text if ok and text else None


class ChangePasswordDialog(QDialog):
    def __init__(self, service, parent=None):
        super().__init__(parent)
        self.service = service
        self.setWindowTitle("Change password")
        self.old, self.new, self.new2 = (QLineEdit() for _ in range(3))
        for f in (self.old, self.new, self.new2):
            f.setEchoMode(QLineEdit.EchoMode.Password)
        self.error = QLabel()
        self.error.setStyleSheet("color: #c9483c;")
        form = QFormLayout()
        form.addRow("Current password", self.old)
        form.addRow("New password", self.new)
        form.addRow("Confirm", self.new2)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._apply)
        buttons.rejected.connect(self.reject)
        lay = QVBoxLayout(self)
        lay.addLayout(form)
        lay.addWidget(self.error)
        lay.addWidget(buttons)

    def _apply(self) -> None:
        if len(self.new.text()) < MIN_PASSWORD:
            self.error.setText(f"The new password needs at least {MIN_PASSWORD} characters.")
            return
        if self.new.text() != self.new2.text():
            self.error.setText("The new passwords do not match.")
            return
        try:
            self.service.change_password(self.old.text(), self.new.text())
        except WrongPasswordError:
            self.error.setText("The current password is wrong.")
            return
        QMessageBox.information(self, "Change password", "Your password has been changed.")
        self.accept()


class SettingsDialog(QDialog):
    def __init__(self, service, runner, parent=None):
        super().__init__(parent)
        self.service = service
        self.runner = runner
        self.setWindowTitle("Settings")
        self.setMinimumWidth(520)

        self.model = QComboBox()
        for name in service.available_recognizers():
            spec = config.MODELS[name]
            self.model.addItem(f"{spec.label}", name)
        self.model.setCurrentIndex(max(0, self.model.findData(service.recognizer)))
        self.autolock = QSpinBox()
        self.autolock.setRange(1, 60)
        self.autolock.setSuffix(" min")
        self.autolock.setValue(int(service.settings.get("auto_lock_minutes", 5)))
        self.clear_clip = QCheckBox("Clear the clipboard after searching a pasted photo")
        self.clear_clip.setChecked(bool(service.settings.get("clear_clipboard_after_search")))

        form = QFormLayout()
        form.addRow("Recognition model", self.model)
        form.addRow("", muted("Changing the model re-computes the face index in the background. "
                              "Past search results are kept as they were."))
        form.addRow("Lock after idle", self.autolock)
        form.addRow("", self.clear_clip)

        pw_btn = QPushButton("Change password…")
        pw_btn.clicked.connect(lambda: ChangePasswordDialog(service, self).exec())
        questions_btn = QPushButton("Security questions…")
        questions_btn.clicked.connect(self._set_questions)
        code_btn = QPushButton("Recovery code…")
        code_btn.clicked.connect(self._new_code)
        verify_btn = QPushButton("Check database integrity")
        verify_btn.clicked.connect(self._verify)
        sec = QHBoxLayout()
        sec.addWidget(pw_btn)
        sec.addWidget(questions_btn)
        sec.addWidget(code_btn)
        sec.addWidget(verify_btn)
        sec.addStretch(1)
        self.recovery = muted()
        self.recovery.setWordWrap(True)
        self._update_recovery()
        from .. import netguard
        network = muted(f"Network access is blocked by {netguard.status()}.")
        network.setWordWrap(True)
        where = muted(f"Database: {service.vault_name} — {service.vault_root}")
        where.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        where.setWordWrap(True)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        lay = QVBoxLayout(self)
        lay.addWidget(heading("Settings"))
        lay.addLayout(form)
        lay.addSpacing(10)
        lay.addWidget(heading("Security", 13))
        lay.addWidget(self.recovery)
        lay.addLayout(sec)
        lay.addWidget(network)
        lay.addWidget(where)
        lay.addWidget(buttons)

    @property
    def chosen_model(self) -> str:
        return self.model.currentData()

    def apply_simple_settings(self) -> None:
        self.service.set_setting("auto_lock_minutes", self.autolock.value())
        self.service.set_setting("clear_clipboard_after_search", self.clear_clip.isChecked())

    def _update_recovery(self) -> None:
        info = self.service.inspect()
        parts = []
        if info.questions:
            parts.append(f"security questions ({info.required} of {len(info.questions)} answers needed)")
        if info.has_code:
            parts.append("a recovery code")
        text = "This database can be recovered with " + " and ".join(parts) + "." if parts else (
            "⚠ This database has no way to recover a forgotten password. Set security questions below.")
        self.recovery.setText(text)

    def _set_questions(self) -> None:
        pw = ask_password(self, "Security questions", "Setting security questions needs the current password:")
        if not pw:
            return
        try:
            self.service.check_password(pw)
        except WrongPasswordError:
            QMessageBox.warning(self, "Security questions", "Wrong password.")
            return
        if QuestionsDialog(self.service, pw, self.runner, self).exec():
            self._update_recovery()
            QMessageBox.information(self, "Security questions", "Your security questions have been saved.")

    def _new_code(self) -> None:
        pw = ask_password(self, "Recovery code",
                          "A new recovery code is created and the old one stops working.\n\nCurrent password:")
        if not pw:
            return
        try:
            code = self.service.new_recovery_code(pw)
        except WrongPasswordError:
            QMessageBox.warning(self, "Recovery code", "Wrong password.")
            return
        RecoveryCodeDialog(code, self).exec()
        self._update_recovery()

    def _verify(self) -> None:
        problems = self.service.vault.verify()
        if problems:
            QMessageBox.warning(self, "Database integrity", f"{len(problems)} file(s) could not be verified:\n\n"
                                + "\n".join(problems[:10]))
        else:
            QMessageBox.information(self, "Database integrity", "All files decrypted and verified successfully.")
