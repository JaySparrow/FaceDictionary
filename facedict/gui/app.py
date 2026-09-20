"""Start the GUI."""

from __future__ import annotations

import sys
import traceback
from pathlib import Path

from PySide6.QtWidgets import QApplication, QMessageBox

from ..service import FaceDictionaryService
from ..vault import VaultLockedError
from .common import STYLE
from .main_window import MainWindow


def _excepthook(exc_type, exc, tb) -> None:
    if issubclass(exc_type, VaultLockedError):
        return                                   # an action finished after the app locked itself
    traceback.print_exception(exc_type, exc, tb)
    if QApplication.instance() is not None:
        QMessageBox.critical(None, "FaceDictionary", f"Unexpected error: {exc}")


def create_app() -> QApplication:
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName("FaceDictionary")
    app.setOrganizationName("FaceDictionary")
    app.setOrganizationDomain("facedictionary.local")
    app.setStyleSheet(STYLE)
    return app


def run(vault: Path | None = None) -> int:
    app = create_app()
    sys.excepthook = _excepthook
    window = MainWindow(FaceDictionaryService(vault))
    window.show()
    window.raise_()
    return app.exec()
