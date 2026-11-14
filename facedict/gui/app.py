"""Start the GUI."""

from __future__ import annotations

import ctypes
import ctypes.util
import sys
import traceback
from pathlib import Path

from PySide6.QtWidgets import QApplication, QMessageBox

from ..service import FaceDictionaryService
from ..vault import VaultLockedError
from .common import follow_color_scheme
from .main_window import MainWindow


def _excepthook(exc_type, exc, tb) -> None:
    if issubclass(exc_type, VaultLockedError):
        return                                   # an action finished after the app locked itself
    traceback.print_exception(exc_type, exc, tb)
    if QApplication.instance() is not None:
        QMessageBox.critical(None, "FaceDictionary", f"Unexpected error: {exc}")


def allow_dark_mode() -> None:
    """Follow the system's light or dark mode even where a global `NSRequiresAquaSystemAppearance` keeps
    apps light (a common tweak for a dark menu bar only). Same as passing `-NSRequiresAquaSystemAppearance
    NO`, for this process only; must run before the QApplication exists."""
    objc = ctypes.cdll.LoadLibrary(ctypes.util.find_library("objc"))
    ctypes.cdll.LoadLibrary(ctypes.util.find_library("Foundation"))
    objc.objc_getClass.restype = objc.sel_registerName.restype = ctypes.c_void_p
    send = objc.objc_msgSend

    def call(obj, selector: str, *args):
        send.restype = ctypes.c_void_p
        send.argtypes = [ctypes.c_void_p, ctypes.c_void_p] + [type(a) for a in args]
        return send(obj, objc.sel_registerName(selector.encode()), *args)

    def nsstring(text: str):
        return call(objc.objc_getClass(b"NSString"), "stringWithUTF8String:", ctypes.c_char_p(text.encode()))

    no = call(objc.objc_getClass(b"NSNumber"), "numberWithBool:", ctypes.c_bool(False))
    domain = call(objc.objc_getClass(b"NSDictionary"), "dictionaryWithObject:forKey:", ctypes.c_void_p(no),
                  ctypes.c_void_p(nsstring("NSRequiresAquaSystemAppearance")))
    defaults = call(objc.objc_getClass(b"NSUserDefaults"), "standardUserDefaults")
    call(defaults, "setVolatileDomain:forName:", ctypes.c_void_p(domain), ctypes.c_void_p(nsstring("NSArgumentDomain")))


def create_app() -> QApplication:
    if sys.platform == "darwin" and QApplication.instance() is None:
        try:
            allow_dark_mode()
        except Exception:                        # no harm done: the app just stays as the system has it
            traceback.print_exc()
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName("FaceDictionary")
    app.setOrganizationName("FaceDictionary")
    app.setOrganizationDomain("facedictionary.local")
    follow_color_scheme(app)
    return app


def run(vault: Path | None = None) -> int:
    app = create_app()
    sys.excepthook = _excepthook
    window = MainWindow(FaceDictionaryService(vault))
    window.show()
    window.raise_()
    return app.exec()
