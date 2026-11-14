"""M4: GUI flows (offscreen). Uses public LFW photos and a temp vault only."""

from __future__ import annotations

import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest
from PySide6.QtCore import QPoint, QSettings, Qt, QTimer
from PySide6.QtWidgets import QApplication, QDialog, QFileDialog, QLineEdit

from facedict.gui import common, library_page, main_window
from facedict.gui.common import STYLE
from facedict.gui.main_window import MainWindow
from facedict.service import FaceDictionaryService
from tests.conftest import lfw_people

PW = "gui-test-password"
N = 8


@pytest.fixture(autouse=True)
def app_names(qapp):
    qapp.setApplicationName("FaceDictionaryTest")
    qapp.setOrganizationName("FaceDictionaryTest")
    qapp.setOrganizationDomain("facedictionary-test.local")
    qapp.setStyleSheet(STYLE)
    yield
    QSettings().clear()


@pytest.fixture
def people():
    return dict(list(lfw_people(min_images=5).items())[:N])


@pytest.fixture
def win(qtbot, isolated_vault, models_available):
    svc = FaceDictionaryService(isolated_vault)
    w = MainWindow(svc)
    qtbot.addWidget(w)
    w.resize(1280, 820)
    w.show()
    yield w
    w.runner.cancel_all_and_wait()


@pytest.fixture
def ready(win, qtbot, people, tmp_path):
    """Unlocked window with a small library (all but the last photo of each person)."""
    src = tmp_path / "import"
    for name, imgs in people.items():
        (src / name).mkdir(parents=True)
        for p in imgs[:-1]:
            shutil.copy(p, src / name / p.name)
    win.service.create_vault(PW, kdf_n=2 ** 12)
    win.service.import_folder(src)
    win._on_unlocked()
    qtbot.waitUntil(lambda: not win.runner.busy, timeout=30000)
    return win


def held_out(people, i=0):
    name = list(people)[i]
    return name.replace("_", " "), people[name][-1].read_bytes()


def run_search(win, qtbot, data):
    win.search.search(data)
    qtbot.waitUntil(lambda: len(win.search.results.cards()) == 5, timeout=20000)


def open_inline(qtbot, inline):
    """Double-click a piece of text of the entry page (a click is enough where it is empty)."""
    qtbot.mouseDClick(inline.label, Qt.MouseButton.LeftButton, pos=QPoint(6, 6))
    assert inline.editing and inline.editor.isVisible()


def type_inline(qtbot, inline, text: str, finish: str = "enter"):
    """Open a text, replace what it says and close it: "enter" (⌘Enter for several lines), "esc",
    or "click" (somewhere else on the page)."""
    open_inline(qtbot, inline)
    if inline.multiline:
        inline.editor.setPlainText(text)
    else:
        inline.editor.setText(text)                    # QTest cannot type CJK; an IME would
    if finish == "enter":
        mod = Qt.KeyboardModifier.ControlModifier if inline.multiline else Qt.KeyboardModifier.NoModifier
        qtbot.keyClick(inline.editor, Qt.Key.Key_Return, mod)
    elif finish == "esc":
        qtbot.keyClick(inline.editor, Qt.Key.Key_Escape)
    else:
        page = inline.window()
        qtbot.mouseClick(page.library.stamps, Qt.MouseButton.LeftButton)
    assert not inline.editing


def idle(win, qtbot):
    qtbot.waitUntil(lambda: not win.runner.busy, timeout=20000)


# --- vault creation / unlocking ---------------------------------------------------------------

def test_create_vault_then_security_questions(win, qtbot, monkeypatch):
    from facedict.gui import questions_dialog
    shown = []
    monkeypatch.setattr(questions_dialog.QuestionsDialog, "exec", lambda self: shown.append(self.password) or 1)
    assert win.stack.currentIndex() == 0 and win.lock_screen.pages.currentIndex() == 0
    ls = win.lock_screen
    ls.new_pw.setText("short")
    ls.new_pw2.setText("short")
    ls.create_btn.click()
    assert "at least" in ls.error.text() and not win.service.vault_exists()
    ls.new_pw.setText(PW)
    ls.new_pw2.setText(PW + "x")
    ls.create_btn.click()
    assert "do not match" in ls.error.text()
    ls.new_pw2.setText(PW)
    ls.create_btn.click()
    qtbot.waitUntil(lambda: win.stack.currentIndex() == 1, timeout=20000)
    qtbot.waitUntil(lambda: bool(shown), timeout=5000)
    assert shown == [PW]                                    # the questions dialog gets the new password
    assert win.service.unlocked and win.service.vault_exists()
    assert ls.new_pw.text() == "" and ls.new_pw2.text() == ""
    assert win.windowTitle().endswith(win.service.vault_name)


def test_wrong_password_then_unlock(ready, qtbot):
    ready.lock()
    ls = ready.lock_screen
    assert ls.pages.currentIndex() == 1
    ls.pw.setText("wrong password")
    ls.unlock_btn.click()
    qtbot.waitUntil(lambda: "Wrong password" in ls.error.text(), timeout=20000)
    assert not ready.service.unlocked and ls.pw.text() == ""
    qtbot.waitUntil(lambda: ls.unlock_btn.isEnabled(), timeout=3000)
    ls.pw.setText(PW)
    ls.unlock_btn.click()
    qtbot.waitUntil(lambda: ready.stack.currentIndex() == 1, timeout=20000)
    assert ready.service.unlocked


# --- search, confirm, history, library -----------------------------------------------------------

def test_search_confirm_and_history(ready, qtbot, people, monkeypatch):
    name, data = held_out(people, 1)
    run_search(ready, qtbot, data)
    cards = ready.search.results.cards()
    assert [c.entry_id for c in cards][0] == ready.service.find_entry_by_name(name).id
    entry_id = cards[0].entry_id
    n_photos = len(ready.service.get_entry(entry_id).photos)
    cards[0].confirm_btn.click()
    qtbot.waitUntil(lambda: ready.search.results.record.confirmed is not None, timeout=10000)
    assert len(ready.service.get_entry(entry_id).photos) == n_photos + 1

    # history shows the stored result without recomputing anything
    monkeypatch.setattr(ready.service.engine(), "embed", lambda *a, **k: pytest.fail("recomputed"))
    monkeypatch.setattr(ready.service.engine(), "detect", lambda *a, **k: pytest.fail("recomputed"))
    ready.nav.setCurrentRow(1)
    qtbot.waitUntil(lambda: ready.history.list.count() == 1, timeout=5000)
    ready.history.list.setCurrentRow(0)
    qtbot.waitUntil(lambda: len(ready.history.results.cards()) == 5, timeout=5000)
    qtbot.waitUntil(lambda: not ready.runner.busy, timeout=10000)
    assert ready.history.results.record.confirmed.entry_id == entry_id

    # undo from history -> the album loses the photo again
    monkeypatch.undo()
    ready.history.results._undo()
    qtbot.waitUntil(lambda: ready.history.results.record.confirmed is None, timeout=10000)
    assert len(ready.service.get_entry(entry_id).photos) == n_photos


def test_open_entry_from_result(ready, qtbot, people):
    name, data = held_out(people, 2)
    run_search(ready, qtbot, data)
    ready.search.results.cards()[0].open_entry()          # clicking the thumbnail or the name does this
    assert ready.page_stack.currentWidget() is ready.library
    assert ready.library.entry is not None and ready.library.entry.name == name


def test_no_face_photo(ready, qtbot):
    from tests.conftest import SAMPLES
    ready.search.search((SAMPLES / "no_face.jpg").read_bytes())
    qtbot.waitUntil(lambda: "No face" in ready.search.drop.view.info.text(), timeout=10000)
    assert ready.search.results.cards() == []
    assert ready.service.list_history() == []


# --- locking ------------------------------------------------------------------------------------------

def test_lock_clears_all_personal_data_from_the_ui(ready, qtbot, people):
    run_search(ready, qtbot, held_out(people)[1])
    ready.nav.setCurrentRow(2)
    ready.library.list.setCurrentRow(0)
    assert ready.library.photos.count() > 0
    ready.lock()
    assert not ready.service.unlocked and ready.stack.currentIndex() == 0
    assert ready.search.results.cards() == [] and ready.search.results.record is None
    view = ready.search.drop.view
    assert not view.image.has_image and view.image.face_count == 0 and view.strip.buttons == []
    assert ready.library.list.count() == 0 and ready.library.photos.count() == 0
    assert ready.library.title.text == "" and ready.library.title.editor.text() == ""
    assert ready.history.list.count() == 0
    assert ready.windowTitle().endswith("Locked")


def test_auto_lock_after_idle(ready):
    al = ready.autolock
    assert al.limit_seconds == 300
    al.last_activity = time.time() - 200
    al.check()
    assert ready.service.unlocked
    al.last_activity = time.time() - 301
    al.check()
    assert not ready.service.unlocked and ready.stack.currentIndex() == 0
    assert "inactivity" in ready.lock_screen.error.text()


def test_lock_closes_open_dialogs(ready, qtbot):
    dlg = QDialog(ready)
    dlg.show()
    ready.lock()
    assert not dlg.isVisible()


def test_text_being_typed_is_saved_on_lock(ready, qtbot):
    ready.nav.setCurrentRow(2)
    ready.library.list.setCurrentRow(0)
    lib = ready.library
    entry_id = lib.entry.id
    open_inline(qtbot, lib.title)
    lib.title.editor.end(False)
    qtbot.keyClicks(lib.title.editor, " (tennis)")
    assert lib.has_unsaved_changes
    ready.lock()
    assert lib.undo_stack.count() == 0                       # the undo history is forgotten too
    ready.service.unlock(PW)
    assert ready.service.get_entry(entry_id).name.endswith(" (tennis)")


# --- library actions --------------------------------------------------------------------------------

def test_edit_delete_restore_entry(ready, qtbot, monkeypatch):
    monkeypatch.setattr(library_page, "confirm", lambda *a, **k: True)
    ready.nav.setCurrentRow(2)
    ready.library.list.setCurrentRow(0)
    lib = ready.library
    entry = lib.entry
    assert not lib.aliases_text.editing and lib.aliases_text.empty      # text reads like a label first
    assert lib.aliases_text.label.text() == "Aliases"
    type_inline(qtbot, lib.aliases_text, "Nick, Nicky")
    assert ready.service.get_entry(entry.id).aliases == ["Nick", "Nicky"]
    assert lib.aliases_text.label.full_text == "·  Nick  ·  Nicky"
    n = lib.list.count()
    lib.delete_entry()
    assert ready.library.list.count() == n - 1
    ready.nav.setCurrentRow(3)
    assert ready.trash.tree.topLevelItemCount() == 1
    ready.trash.tree.topLevelItem(0).setSelected(True)
    ready.trash.restore()
    qtbot.waitUntil(lambda: ready.trash.tree.topLevelItemCount() == 0, timeout=10000)
    assert ready.service.get_entry(entry.id).name == entry.name


def test_import_folder_via_gui(win, qtbot, people, tmp_path, monkeypatch):
    win.service.create_vault(PW, kdf_n=2 ** 12)
    win._on_unlocked()
    src = tmp_path / "people"
    for name, imgs in list(people.items())[:3]:
        (src / name).mkdir(parents=True)
        for p in imgs[:2]:
            shutil.copy(p, src / name / p.name)
    monkeypatch.setattr(library_page, "confirm", lambda *a, **k: True)
    monkeypatch.setattr(library_page, "choose_folder", lambda *a, **k: str(src))
    reports = []
    monkeypatch.setattr(library_page.QMessageBox, "information", lambda *a, **k: reports.append(a[2]))
    win.nav.setCurrentRow(2)
    win.library.import_folder()
    qtbot.waitUntil(lambda: bool(reports), timeout=30000)
    assert "New entries: 3" in reports[0] and "Photos added: 6" in reports[0]
    assert win.library.list.count() == 3


def test_switch_model_from_settings(ready, qtbot, monkeypatch, people):
    monkeypatch.setattr(main_window.SettingsDialog, "exec", lambda self: QDialog.DialogCode.Accepted)
    monkeypatch.setattr(main_window.SettingsDialog, "chosen_model", property(lambda self: "sface"))
    ready.open_settings()
    qtbot.waitUntil(lambda: ready.service.recognizer == "sface" and not ready.runner.busy, timeout=60000)
    assert ready.service.index.dim == 128
    name, data = held_out(people, 3)
    run_search(ready, qtbot, data)
    assert ready.search.results.record.model == "sface"


# --- privacy: no traces --------------------------------------------------------------------------------

def test_file_dialog_leaves_no_folder_history(ready, qtbot, tmp_path, people):
    secret_dir = tmp_path / "CANARY-dialog-7f3a"
    secret_dir.mkdir()
    photo = secret_dir / "someone.jpg"
    shutil.copy(next(iter(people.values()))[0], photo)

    def drive():
        dlg = QApplication.activeModalWidget()
        if not isinstance(dlg, QFileDialog):
            QTimer.singleShot(100, drive)
            return
        dlg.setDirectory(str(secret_dir))
        dlg.findChild(QLineEdit, "fileNameEdit").setText(str(photo))
        dlg.accept()

    QTimer.singleShot(200, drive)
    paths = common.open_images(ready, "test", multiple=False)
    assert [Path(p).resolve() for p in paths] == [photo.resolve()]
    # Qt's own dialog records visited folders in QSettings("QtProject"); they must be gone
    qs = QSettings(QSettings.Scope.UserScope, "QtProject")
    qs.sync()
    qs.beginGroup("FileDialog")
    stored = " ".join(str(qs.value(k)) for k in qs.childKeys())
    assert "CANARY-dialog-7f3a" not in stored
    if sys.platform == "darwin":                           # ask cfprefsd, the authoritative store
        out = subprocess.run(["defaults", "read", "com.qtproject"], capture_output=True, text=True).stdout
        assert "CANARY-dialog-7f3a" not in out


def test_app_settings_only_store_window_geometry(ready):
    ready.close()
    s = QSettings()
    s.setFallbacksEnabled(False)                           # ignore macOS global-domain keys
    keys = set(s.allKeys())
    assert keys <= {"geometry", "appearance"}, f"{len(keys)} unexpected keys"     # window place, light/dark


# --- choosing a face in a group photo --------------------------------------------------------------

@pytest.fixture
def group(ready, people):
    from tests.conftest import group_photo, single_face_photos
    names = single_face_photos(people, 3)
    data = group_photo([people[n][-1] for n in names], [170, 120, 240])      # the third face is the largest
    return data, [ready.service.find_entry_by_name(n.replace("_", " ")).id for n in names]


def click_face(qtbot, view, index):
    from PySide6.QtCore import Qt as QtNs
    pos = view.face_rect(index).center().toPoint()
    qtbot.mouseClick(view, QtNs.MouseButton.LeftButton, pos=pos)


def test_click_a_face_to_see_its_matches(ready, qtbot, group):
    data, expected = group
    run_search(ready, qtbot, data)
    view = ready.search.drop.view
    results = ready.search.results
    assert view.image.face_count == 3 and view.strip.isVisible() and len(view.strip.buttons) == 3
    assert results.face_index == 2 and results.cards()[0].entry_id == expected[2]     # largest first
    assert "face 3 of 3" in results.caption.text()
    hid = results.record.id

    click_face(qtbot, view.image, 0)                        # click the box in the photo
    assert results.face_index == 0 and view.image.selected == 0
    assert results.cards()[0].entry_id == expected[0]
    assert view.strip.buttons[0].isChecked()

    view.strip.buttons[1].click()                          # or the thumbnail below the photo
    assert results.face_index == 1 and results.cards()[0].entry_id == expected[1]

    view.image.setFocus()
    qtbot.keyClick(view.image, Qt.Key.Key_Right)          # or the arrow keys
    assert results.face_index == 2
    qtbot.keyClick(view.image, Qt.Key.Key_1)
    assert results.face_index == 0
    qtbot.waitUntil(lambda: ready.service.get_history(hid).selected == 0, timeout=5000)


def test_hovering_a_face_highlights_it(ready, qtbot, group):
    data, _ = group
    run_search(ready, qtbot, data)
    img = ready.search.drop.view.image
    qtbot.mouseMove(img, img.face_rect(1).center().toPoint())
    qtbot.waitUntil(lambda: img._hover == 1, timeout=2000)
    assert img.toolTip() == ""                               # no tooltips, just the yellow box
    qtbot.mouseMove(img, img.image_rect().topLeft().toPoint() + QPoint(2, 2))
    qtbot.waitUntil(lambda: img._hover == -1, timeout=2000)


def test_confirm_each_face_separately(ready, qtbot, group):
    data, expected = group
    run_search(ready, qtbot, data)
    view, results = ready.search.drop.view, ready.search.results
    click_face(qtbot, view.image, 0)
    results.cards()[0].confirm_btn.click()
    qtbot.waitUntil(lambda: results.record.search(0).confirmed is not None, timeout=10000)
    assert results.face_index == 0
    click_face(qtbot, view.image, 1)                         # another face is still open for confirmation
    assert results.cards()[0].confirm_btn.isEnabled()
    results.cards()[0].confirm_btn.click()
    qtbot.waitUntil(lambda: results.record.search(1).confirmed is not None, timeout=10000)
    rec = ready.service.get_history(results.record.id)
    assert rec.search(0).confirmed.entry_id == expected[0]
    assert rec.search(1).confirmed.entry_id == expected[1]
    assert rec.search(2).confirmed is None
    photo = ready.service.get_entry(expected[1]).photo(rec.search(1).confirmed.added_photo_id)
    assert photo.face == rec.search(1).face


def test_history_page_switches_faces(ready, qtbot, group):
    data, expected = group
    run_search(ready, qtbot, data)
    ready.nav.setCurrentRow(1)
    qtbot.waitUntil(lambda: ready.history.list.count() == 1, timeout=5000)
    ready.history.list.setCurrentRow(0)
    assert "(3 faces)" in ready.history.list.item(0).text()
    qtbot.waitUntil(lambda: ready.history.query.image.face_count == 3, timeout=10000)
    icon = ready.history.list.item(0).icon().cacheKey()
    click_face(qtbot, ready.history.query.image, 1)
    assert ready.history.results.cards()[0].entry_id == expected[1]
    qtbot.waitUntil(lambda: ready.service.list_history()[0].selected == 1, timeout=5000)
    qtbot.waitUntil(lambda: ready.history.list.item(0).icon().cacheKey() != icon, timeout=5000)
    assert ready.service.get_entry(expected[1]).name in ready.history.list.item(0).text()   # its top match


def dclick_face(qtbot, view, index):
    qtbot.mouseDClick(view, Qt.MouseButton.LeftButton, pos=view.face_rect(index).center().toPoint())


def test_photo_dialog_double_click_picks_the_face_used_for_matching(ready, qtbot, group):
    """Double-clicking a photo opens it big; there a single click on a face changes nothing (no
    accidents), a double-click uses that face at once, and ⌘Z goes back to the previous one."""
    from facedict.gui.library_page import PhotoDialog
    data, expected = group
    entry = ready.service.get_entry(expected[1])
    res = ready.service.add_photo(entry.id, data)
    lib = open_entry(ready)
    lib.select_entry(entry.id)
    img, faces = ready.service.detect_faces(res.photo.photo_id)
    dlg = PhotoDialog(ready.service, ready.runner, entry.id, res.photo, img, faces, ready)
    dlg.face_changed.connect(lambda pid, old, new: lib._faces_changed(entry.id, [(pid, old, new)]))
    qtbot.addWidget(dlg)
    dlg.show()
    qtbot.waitExposed(dlg)
    assert dlg.view.image.interactive and dlg.view.image.double_click
    assert dlg.view.image.selected == 2                     # the largest face, chosen when the photo was added
    assert dlg.view.info.isHidden()                         # nothing written under the photo

    def stored_face():
        return ready.service.get_entry(entry.id).photo(res.photo.photo_id).face

    before = stored_face()
    click_face(qtbot, dlg.view.image, 0)                    # a single click: nothing happens
    qtbot.mouseClick(dlg.view.strip.buttons[0], Qt.MouseButton.LeftButton)     # nor on the thumbnails
    dlg.view.strip.buttons[0].click()
    qtbot.wait(200)
    assert stored_face() == before and dlg.view.image.selected == 2

    dclick_face(qtbot, dlg.view.image, 1)                   # a double-click is the choice
    qtbot.waitUntil(lambda: abs(stored_face().bbox[0] - dlg.faces[1].bbox[0]) < 1, timeout=10000)
    qtbot.waitUntil(lambda: ready.undo_action.text() == "Undo Change Face", timeout=5000)
    qtbot.mouseDClick(dlg.view.strip.buttons[0], Qt.MouseButton.LeftButton)   # or double-click a thumbnail
    qtbot.waitUntil(lambda: abs(stored_face().bbox[0] - dlg.faces[0].bbox[0]) < 1, timeout=10000)
    dlg.close()
    idle(ready, qtbot)
    ready.undo_action.trigger()
    assert abs(stored_face().bbox[0] - dlg.faces[1].bbox[0]) < 1
    ready.undo_action.trigger()                                  # step by step back to the first choice
    assert abs(stored_face().bbox[0] - before.bbox[0]) < 1


def test_the_no_entry_button_excludes_a_photo_from_matching(ready, qtbot):
    ready.nav.setCurrentRow(2)
    ready.library.list.setCurrentRow(0)
    lib = ready.library
    lib.photos.setCurrentRow(0)
    pid = lib.entry.photos[0].photo_id
    assert pid in ready.service.index and not lib.exclude_btn.isChecked()
    assert lib.exclude_btn.isVisible()                      # always there: no edit mode
    lib.exclude_btn.click()
    qtbot.waitUntil(lambda: lib.entry.photo(pid).face is None, timeout=10000)   # after the page refreshed
    assert pid not in ready.service.index
    lib.photos.setCurrentRow(0)
    assert lib.exclude_btn.isChecked() and "again" in lib.exclude_btn.toolTip()
    lib.exclude_btn.click()                                 # and back on
    qtbot.waitUntil(lambda: lib.entry.photo(pid).face is not None, timeout=10000)
    assert pid in ready.service.index
    lib.photos.setCurrentRow(0)
    assert not lib.exclude_btn.isChecked()
    idle(ready, qtbot)
    ready.undo_action.trigger()                             # ⌘Z: switched off again
    assert ready.service.get_entry(lib.entry.id).photo(pid).face is None and pid not in ready.service.index
    ready.redo_action.trigger()                             # ⇧⌘Z: and on
    assert ready.service.get_entry(lib.entry.id).photo(pid).face is not None and pid in ready.service.index


def test_clicking_the_empty_search_zone_opens_a_photo(ready, qtbot, people, monkeypatch, tmp_path):
    from facedict.gui import common as gui_common
    from facedict.gui import search_page
    name, data = held_out(people, 0)
    photo = tmp_path / "q.jpg"
    photo.write_bytes(data)
    monkeypatch.setattr(search_page, "open_images", lambda *a, **k: [str(photo)])
    assert gui_common.open_images is not search_page.open_images or True
    qtbot.mouseClick(ready.search.drop, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: len(ready.search.results.cards()) == 5, timeout=20000)
    assert ready.search.results.cards()[0].name_text() == name
    # once a photo is shown, clicking the zone must not replace it (faces are clickable there)
    before = ready.search.results.record.id
    qtbot.mouseClick(ready.search.drop, Qt.MouseButton.LeftButton)
    qtbot.wait(200)
    assert ready.search.results.record.id == before


# --- several databases ---------------------------------------------------------------------------

QUESTIONS = ["First pet?", "First school?", "Favourite dish?"]
ANSWERS = ["Mimi", "St. Mary's", "Ramen"]


def test_unlock_screen_switches_between_databases(ready, qtbot, tmp_path):
    from facedict.vault import Vault
    other = tmp_path / "Work.noindex"
    Vault.create(other, "the other password", kdf_n=2 ** 12)
    ready.lock()
    ls = ready.lock_screen
    assert ls.bar.name.text() == ready.service.vault_name

    ls.open_database(other)                                  # as the menu does
    assert ls.pages.currentIndex() == 1 and ls.bar.name.text() == "Work"
    ls.pw.setText(PW)                                        # the first database's password must not work
    ls.unlock_btn.click()
    qtbot.waitUntil(lambda: "Wrong password" in ls.error.text(), timeout=20000)
    qtbot.waitUntil(lambda: ls.unlock_btn.isEnabled(), timeout=3000)
    ls.pw.setText("the other password")
    ls.unlock_btn.click()
    qtbot.waitUntil(lambda: ready.stack.currentIndex() == 1, timeout=20000)
    ready.nav.setCurrentRow(2)                               # the library page refreshes when shown
    assert ready.service.vault_root == other.resolve() and ready.library.list.count() == 0
    assert [r.name for r in ready.service.recent_databases()][0] == "Work"

    ready.lock()                                             # and back again, from the remembered list
    ls.open_database(ready.service.recent_databases()[1].path)
    ls.pw.setText(PW)
    ls.unlock_btn.click()
    qtbot.waitUntil(lambda: ready.stack.currentIndex() == 1, timeout=20000)
    ready.nav.setCurrentRow(2)
    assert ready.library.list.count() == N


def test_unlock_screen_offers_to_create_and_reports_bad_folders(ready, qtbot, tmp_path):
    ls = ready.lock_screen
    ready.lock()
    ls.open_database(tmp_path / "brand new.noindex")
    assert ls.pages.currentIndex() == 0                      # empty/missing folder -> create page
    not_a_db = tmp_path / "holiday photos"
    not_a_db.mkdir()
    (not_a_db / "a.jpg").write_bytes(b"x")
    ls.open_database(not_a_db)
    assert ls.pages.currentIndex() == 3 and "not a FaceDictionary database" in ls.problem.text()


def test_recover_the_password_with_security_questions(ready, qtbot):
    ready.service.set_questions(PW, QUESTIONS, ANSWERS, required=2)
    ready.lock()
    ls = ready.lock_screen
    assert ls.forgot_btn.isVisible()
    ls.forgot_btn.click()
    assert ls.pages.currentIndex() == 2 and len(ls.questions_form.fields) == 3
    ls.rec_pw.setText("a fresh password")
    ls.rec_pw2.setText("a fresh password")
    ls.questions_form.fields[0].setText("wrong")
    ls.questions_form.fields[2].setText("nope")
    ls.recover_btn.click()
    qtbot.waitUntil(lambda: "do not match" in ls.error.text(), timeout=20000)
    qtbot.waitUntil(lambda: ls.recover_btn.isEnabled(), timeout=3000)
    ls.questions_form.fields[0].setText(" mimi ")            # capitals and spaces do not matter
    ls.questions_form.fields[2].setText("ramen")
    ls.recover_btn.click()
    qtbot.waitUntil(lambda: ready.stack.currentIndex() == 1, timeout=30000)
    ready.nav.setCurrentRow(2)
    assert ready.service.unlocked and ready.library.list.count() == N
    ready.lock()
    ls.pw.setText("a fresh password")
    ls.unlock_btn.click()
    qtbot.waitUntil(lambda: ready.stack.currentIndex() == 1, timeout=20000)


def test_recovery_page_uses_the_code_when_there_are_no_questions(ready, qtbot):
    code = ready.service.new_recovery_code(PW)
    ready.lock()
    ls = ready.lock_screen
    ls.forgot_btn.click()
    assert ls.questions_form is None and ls.code_field is not None
    ls.code_field.setText(code)
    ls.rec_pw.setText("code made this password")
    ls.rec_pw2.setText("code made this password")
    ls.recover_btn.click()
    qtbot.waitUntil(lambda: ready.stack.currentIndex() == 1, timeout=30000)
    assert ready.service.unlocked


def test_appearance_setting(ready, qtbot):
    from facedict.gui import common, settings_dialog
    dlg = settings_dialog.SettingsDialog(ready.service, ready.runner, ready)
    qtbot.addWidget(dlg)
    assert dlg.appearance.currentData() == "system"
    dlg.appearance.setCurrentIndex(dlg.appearance.findData("dark"))         # applied at once
    assert common.appearance() == "dark"
    dlg.reject()                                                             # Cancel: as it was
    assert common.appearance() == "system"
    dlg = settings_dialog.SettingsDialog(ready.service, ready.runner, ready)
    qtbot.addWidget(dlg)
    dlg.appearance.setCurrentIndex(dlg.appearance.findData("light"))
    dlg.accept()
    assert common.appearance() == "light"
    common.set_appearance("system")


def test_settings_show_and_set_recovery_methods(ready, qtbot, monkeypatch):
    from facedict.gui import settings_dialog
    dlg = settings_dialog.SettingsDialog(ready.service, ready.runner, ready)
    qtbot.addWidget(dlg)
    assert "no way to recover" in dlg.recovery.text()
    monkeypatch.setattr(settings_dialog, "ask_password", lambda *a, **k: PW)
    saved = {}

    def fake_questions(service, password, runner, parent=None, first_time=False):
        saved["password"] = password
        service.set_questions(password, QUESTIONS, ANSWERS, 2)

        class Fake:
            def exec(self):
                return 1
        return Fake()

    monkeypatch.setattr(settings_dialog, "QuestionsDialog", fake_questions)
    monkeypatch.setattr(settings_dialog.QMessageBox, "information", lambda *a, **k: None)
    dlg._set_questions()
    assert saved["password"] == PW and "security questions (2 of 3" in dlg.recovery.text()


# --- entry page: editing in place, fields ----------------------------------------------------------

def open_entry(ready, row: int = 0):
    ready.nav.setCurrentRow(2)
    ready.library.list.setCurrentRow(row)
    return ready.library


def add_field(qtbot, lib, kind: str, name: str, value: str | None = None):
    """Add Field to All Entries / to This Entry (right-click, Edit menu, ⇧⌘; / ⌘;): type the name,
    Enter, then the text (⌘Enter)."""
    lib.add_actions[kind].trigger()
    row = next(r for r in lib.body_holder.findChildren(library_page.FieldRow) if r.name.editing)
    row.name.editor.setText(name)
    qtbot.keyClick(row.name.editor, Qt.Key.Key_Return)
    key = next(k for k, r in lib._rows.items() if r.field_name == name)
    qtbot.waitUntil(lambda: lib._rows[key].value.editing, timeout=3000)     # Enter goes on to the text
    value_box = lib._rows[key].value
    if value is None:
        qtbot.keyClick(value_box.editor, Qt.Key.Key_Escape)
    else:
        value_box.editor.setPlainText(value)
        qtbot.keyClick(value_box.editor, Qt.Key.Key_Return, Qt.KeyboardModifier.ControlModifier)
    return key


def test_shared_and_own_fields_in_the_entry_page(ready, qtbot):
    lib = open_entry(ready)
    entry_id = lib.entry.id
    met = add_field(qtbot, lib, "shared", "Met at", "the 2019 conference")
    phone = add_field(qtbot, lib, "own", "Phone", "555-11")
    stored = ready.service.get_entry(entry_id)
    assert met[0] == "field" and stored.fields[met[1]] == "the 2019 conference"
    assert [(f.name, f.value) for f in stored.own] == [("Phone", "555-11")] and phone == ("own", "Phone")

    # the shared field is on every entry: its text is dimmed while empty (its name is not), a click fills it in
    other = open_entry(ready, 1)
    row = other._rows[met]
    assert row.value.empty and row.value.label.text() == "—" and row.value.label.property("empty")
    assert not row.name.label.property("empty")
    assert ("own", "Phone") not in other._rows
    qtbot.mouseClick(row.value.label, Qt.MouseButton.LeftButton)
    assert row.value.editing
    qtbot.keyClick(row.value.editor, Qt.Key.Key_Escape)

    # names must stay unique; Enter on a taken name keeps the box open
    other.add_actions["own"].trigger()
    new_row = next(r for r in other.body_holder.findChildren(library_page.FieldRow) if r.name.editing)
    new_row.name.editor.setText("met AT")
    qtbot.keyClick(new_row.name.editor, Qt.Key.Key_Return)
    assert new_row.name.editing
    qtbot.keyClick(new_row.name.editor, Qt.Key.Key_Escape)          # Esc: nothing is added
    qtbot.waitUntil(lambda: not any(r.name.editing for r in other.body_holder.findChildren(library_page.FieldRow)))
    assert ready.service.get_entry(other.entry.id).own == []

    # the search box can look inside it
    assert other.scope.itemText(3) == "Met at"
    other.scope.setCurrentIndex(3)
    other.filter.setText("2019 conference")
    visible = [other.list.item(i).data(library_page.ROLE_ID) for i in range(other.list.count())
               if not other.list.item(i).isHidden()]
    assert visible == [entry_id]


def test_renaming_and_removing_fields_can_be_undone(ready, qtbot, monkeypatch):
    monkeypatch.setattr(library_page, "confirm", lambda *a, **k: True)
    lib = open_entry(ready)
    entry_id = lib.entry.id
    city = add_field(qtbot, lib, "shared", "City", "Lisbon")
    type_inline(qtbot, lib._rows[city].name, "Town")                  # double-click a name to rename it
    assert [f.name for f in ready.service.global_fields()] == ["Town"]

    title, remove = lib._rows[city].name.menu_actions[0]                # the right-click menu
    assert title == "Remove “Town”"                                      # says which field goes
    remove()
    assert ready.service.global_fields() == []
    stored = ready.service.get_entry(entry_id)
    assert ("Town", "Lisbon") in [(f.name, f.value) for f in stored.own]  # the text is kept

    ready.undo_action.trigger()                                        # the field is back, with its text
    assert [f.name for f in ready.service.global_fields()] == ["Town"]
    stored = ready.service.get_entry(entry_id)
    assert stored.fields[city[1]] == "Lisbon" and stored.own == []
    ready.undo_action.trigger()
    assert [f.name for f in ready.service.global_fields()] == ["City"]

    own = add_field(qtbot, lib, "own", "Phone", "555")
    lib._rows[own].value.menu_actions[0][1]()                          # right-click › Remove “Phone”
    assert ready.service.get_entry(entry_id).own == []
    ready.undo_action.trigger()
    assert [(f.name, f.value) for f in ready.service.get_entry(entry_id).own] == [("Phone", "555")]


def test_fields_are_added_from_menus_and_one_line_separates_own_fields(ready, qtbot):
    lib = open_entry(ready)
    labels = [a.text() for a in ready.menuBar().actions()[0].menu().actions() if a.text()]
    assert labels[-2:] == ["Add Field to All Entries", "Add Field to This Entry"]       # Edit menu
    assert lib.add_actions["own"].shortcut().toString() == "Ctrl+;"
    from PySide6.QtWidgets import QAbstractButton
    assert lib._divider is None and not lib.body_holder.findChildren(QAbstractButton)     # no buttons there
    lib.add_actions["own"].trigger()                                  # a line appears with the new row
    qtbot.waitUntil(lambda: lib._divider is not None and lib._divider.isVisibleTo(lib), timeout=2000)
    new_row = next(r for r in lib.body_holder.findChildren(library_page.FieldRow) if r.name.editing)
    qtbot.keyClick(new_row.name.editor, Qt.Key.Key_Escape)            # nothing added: and no line
    assert lib._divider is None
    key = add_field(qtbot, lib, "own", "Phone", "555")
    assert lib._divider is not None and lib._rows[key].personal
    ready.undo_action.trigger()
    ready.undo_action.trigger()
    assert lib._divider is None and ready.service.get_entry(lib.entry.id).own == []


def test_aliases_can_be_selected_and_the_status_bar_is_quiet(ready, qtbot):
    lib = open_entry(ready)
    type_inline(qtbot, lib.aliases_text, "Nick")
    assert lib.aliases_text.label.textInteractionFlags() & Qt.TextInteractionFlag.TextSelectableByMouse
    ready._update_status()
    assert ready.stats.text() == ""                                   # no counts, no model name


def test_clearing_a_field_name_removes_the_field(ready, qtbot, monkeypatch):
    lib = open_entry(ready)
    entry_id = lib.entry.id
    phone = add_field(qtbot, lib, "own", "Phone", "555")
    type_inline(qtbot, lib._rows[phone].name, "")                      # no name: no field
    assert ready.service.get_entry(entry_id).own == []
    ready.undo_action.trigger()
    assert [f.name for f in ready.service.get_entry(entry_id).own] == ["Phone"]

    city = add_field(qtbot, lib, "shared", "City", "Lisbon")
    monkeypatch.setattr(library_page, "confirm", lambda *a, **k: False)   # a shared field asks first
    type_inline(qtbot, lib._rows[city].name, "")
    assert [f.name for f in ready.service.global_fields()] == ["City"] and lib._rows[city].name.text == "City"
    monkeypatch.setattr(library_page, "confirm", lambda *a, **k: True)
    type_inline(qtbot, lib._rows[city].name, "")
    assert ready.service.global_fields() == []


def test_a_field_row_is_marked_while_its_menu_is_open(ready, qtbot, monkeypatch):
    from facedict.models import OwnField
    lib = open_entry(ready)
    e = ready.service.get_entry(lib.entry.id)
    e.own.append(OwnField("Phone", "555"))
    ready.service.update_entry(e)
    lib.refresh(e.id)
    row = lib._rows[("own", "Phone")]
    assert not row.value.editing and not row.marked
    seen = []

    def fake_exec(menu, _at):
        seen.append((row.marked, [a.text() for a in menu.actions() if a.text()]))
        return next(a for a in menu.actions() if a.text() == "Remove “Phone”")

    monkeypatch.setattr(library_page, "exec_menu", fake_exec)
    from PySide6.QtGui import QContextMenuEvent
    event = QContextMenuEvent(QContextMenuEvent.Reason.Mouse, QPoint(5, 5))
    row.value.contextMenuEvent(event)
    assert seen[0][0] is True and not row.marked                      # marked only while it is open
    assert seen[0][1][:3] == ["Copy", "Edit…", "Remove “Phone”"]
    qtbot.waitUntil(lambda: ready.service.get_entry(lib.entry.id).own == [], timeout=3000)   # runs afterwards


def test_photos_can_be_dropped_into_the_album_and_o_adds_them(ready, qtbot, people, monkeypatch):
    from PySide6.QtCore import QMimeData, QUrl
    lib = open_entry(ready)
    name = lib.entry.name.replace(" ", "_")
    photo = people[name][-1]
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(str(photo)), QUrl.fromLocalFile(str(photo.parent))])
    assert library_page.dropped_images(mime) == [photo]                # photo files only
    before = lib.photos.count()
    lib.photos.dropped.emit([photo])                                   # what a drop from Finder does
    qtbot.waitUntil(lambda: lib.photos.count() == before + 1, timeout=20000)
    idle(ready, qtbot)
    ready.undo_action.trigger()
    assert lib.photos.count() == before

    monkeypatch.setattr(library_page, "open_images", lambda *a, **k: [str(photo)])
    ready._open_shortcut()                                             # ⌘O on the Library page
    qtbot.waitUntil(lambda: lib.photos.count() == before + 1, timeout=20000)
    assert ready.page_stack.currentWidget() is lib


def test_file_dialogs_start_in_the_last_folder_used(ready, tmp_path, monkeypatch):
    starts = []

    def fake(parent, title, start, *a, **k):
        starts.append(start)
        return [str(tmp_path / "pics" / "a.jpg")], ""

    (tmp_path / "pics").mkdir()
    monkeypatch.setattr(common, "_LAST_FOLDERS", {})
    monkeypatch.setattr(common.QFileDialog, "getOpenFileNames", fake)
    common.open_images(ready, kind="album")
    common.open_images(ready, kind="album")
    common.open_images(ready, kind="search")                           # each kind remembers its own
    assert starts == [str(Path.home()), str(tmp_path / "pics"), str(Path.home())]
    assert "pics" not in " ".join(QSettings().allKeys())                # kept in memory, never saved


def test_the_plus_button_opens_a_new_search_when_a_photo_is_shown(ready, qtbot, people, monkeypatch, tmp_path):
    from facedict.gui import search_page
    run_search(ready, qtbot, held_out(people, 0)[1])
    first = ready.search.results.record.id
    photo = tmp_path / "next.jpg"
    photo.write_bytes(held_out(people, 1)[1])
    monkeypatch.setattr(search_page, "open_images", lambda *a, **k: [str(photo)])
    ready.search.open_btn.click()
    qtbot.waitUntil(lambda: ready.search.results.record is not None and ready.search.results.record.id != first,
                    timeout=20000)


def test_a_photo_has_a_note_of_its_own(ready, qtbot):
    from facedict.gui.library_page import PhotoDialog
    lib = open_entry(ready)
    ref = lib.entry.photos[0]
    img, faces = ready.service.detect_faces(ref.photo_id)
    dlg = PhotoDialog(ready.service, ready.runner, lib.entry.id, ref, img, faces, ready)
    dlg.save_note = lambda pid, old, new: lib._push(
        library_page.undo.SetPhotoNote(ready.service, lib.entry.id, pid, old, new, **lib._callbacks()), light=True)
    qtbot.addWidget(dlg)
    dlg.show()
    qtbot.waitExposed(dlg)
    assert dlg.note.empty and dlg.note.label.text() == "Note"
    open_inline(qtbot, dlg.note)
    dlg.note.editor.setPlainText("At the lake, summer 2019")
    dlg.reject()                                                       # closing keeps what was typed
    stored = ready.service.get_entry(lib.entry.id).photo(ref.photo_id)
    assert stored.note == "At the lake, summer 2019"
    ready.undo_action.trigger()
    assert ready.service.get_entry(lib.entry.id).photo(ref.photo_id).note == ""


def test_the_delete_key_deletes_what_is_selected_on_every_page(ready, qtbot, people, monkeypatch):
    monkeypatch.setattr(library_page, "confirm", lambda *a, **k: True)
    from facedict.gui import history_page, trash_page
    monkeypatch.setattr(history_page, "confirm", lambda *a, **k: True)
    monkeypatch.setattr(trash_page, "confirm", lambda *a, **k: True)
    svc = ready.service
    lib = open_entry(ready)
    entry_id = lib.entry.id
    n_photos = len(lib.entry.photos)

    lib.filter.setFocus()                                              # typing keeps ⌫ for itself
    lib.filter.setText("ab")
    qtbot.keyClick(lib.filter, Qt.Key.Key_Backspace)
    assert lib.filter.text() == "a" and svc.has_entry(entry_id)
    lib.filter.clear()

    lib.photos.setFocus()                                              # the album: the selected photo
    lib.photos.setCurrentRow(1)
    qtbot.keyClick(lib.photos, Qt.Key.Key_Backspace)
    assert len(svc.get_entry(entry_id).photos) == n_photos - 1
    lib.list.setFocus()                                                # the list: the selected entry
    qtbot.keyClick(lib.list, Qt.Key.Key_Delete)
    assert not svc.has_entry(entry_id)

    run_search(ready, qtbot, held_out(people, 1)[1])
    ready.nav.setCurrentRow(1)                                         # History: the selected record
    qtbot.waitUntil(lambda: ready.history.list.count() == 1, timeout=5000)
    ready.history.list.setCurrentRow(0)
    ready.history.list.setFocus()
    qtbot.keyClick(ready.history.list, Qt.Key.Key_Backspace)
    assert svc.list_history() == []

    ready.nav.setCurrentRow(3)                                         # Trash: for good, after asking
    tree = ready.trash.tree
    qtbot.waitUntil(lambda: tree.topLevelItemCount() == 3, timeout=5000)
    tree.topLevelItem(0).setSelected(True)
    tree.setFocus()
    qtbot.keyClick(tree, Qt.Key.Key_Backspace)
    assert len(svc.list_trash()) == 2
    assert [a.text() for a in ready.menuBar().actions()[0].menu().actions()].count("Delete") == 1


# --- several at once (⌘-click, ⇧-click) ---------------------------------------------------------------

CMD = Qt.KeyboardModifier.ControlModifier            # Qt's name for ⌘ on a Mac
SHIFT = Qt.KeyboardModifier.ShiftModifier


def click_row(qtbot, view, row, modifier=Qt.KeyboardModifier.NoModifier):
    rect = view.visualItemRect(view.item(row))
    qtbot.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, modifier, rect.center())


def test_several_entries_are_deleted_and_restored_as_one_step(ready, qtbot, monkeypatch):
    asked, warnings = [], []
    monkeypatch.setattr(library_page, "confirm", lambda *a, **k: asked.append(a[2]) or True)
    monkeypatch.setattr(library_page.QMessageBox, "warning", lambda *a, **k: warnings.append(a[2]))
    svc = ready.service
    lib = open_entry(ready)
    ids = [lib.list.item(i).data(library_page.ROLE_ID) for i in range(3)]
    click_row(qtbot, lib.list, 0)
    click_row(qtbot, lib.list, 2, SHIFT)                                   # a range
    assert lib._selected_ids() == ids
    assert lib.entry is None and lib.entry_widget.isHidden() and lib.placeholder.text() == "3 entries selected"
    click_row(qtbot, lib.list, 1, CMD)                                     # one less
    assert lib._selected_ids() == [ids[0], ids[2]] and lib.trash_action.text() == "Move 2 Entries to Trash"

    lib.list.setFocus()
    qtbot.keyClick(lib.list, Qt.Key.Key_Backspace)
    assert len(asked) == 1 and "2 entries" in asked[0]
    assert not svc.has_entry(ids[0]) and not svc.has_entry(ids[2]) and svc.has_entry(ids[1])
    assert ready.undo_action.text() == "Undo Delete 2 Entries"
    ready.undo_action.trigger()                                            # one ⌘Z brings both back
    assert svc.has_entry(ids[0]) and svc.has_entry(ids[2]) and svc.list_trash() == []
    assert lib._selected_ids() == [ids[0], ids[2]] and lib.placeholder.text() == "2 entries selected"

    ready.redo_action.trigger()
    svc.purge(svc.list_trash()[0].id)                                      # one of them is gone for good
    ready.undo_action.trigger()
    assert warnings and "no longer in the Trash" in warnings[0]
    assert not svc.has_entry(ids[0]) and not svc.has_entry(ids[2])        # so neither comes back


def test_several_entries_are_exported_at_once(ready, qtbot, tmp_path, monkeypatch):
    out = tmp_path / "export"
    out.mkdir()
    monkeypatch.setattr(library_page, "confirm", lambda *a, **k: True)
    monkeypatch.setattr(library_page, "choose_folder", lambda *a, **k: str(out))
    reports = []
    monkeypatch.setattr(library_page.QMessageBox, "information", lambda *a, **k: reports.append(a[2]))
    lib = open_entry(ready)
    click_row(qtbot, lib.list, 0)
    click_row(qtbot, lib.list, 1, CMD)
    assert lib.export_action.text() == "Export 2 Entries…"
    lib.export_action.trigger()                                            # from the right-click menu
    qtbot.waitUntil(lambda: bool(reports), timeout=10000)
    assert len([d for d in out.iterdir() if (d / "entry.json").exists()]) == 2
    second = ready.service.get_entry(lib.list.item(1).data(library_page.ROLE_ID))
    lib.filter.setText(second.name)                                        # what the filter hides is not selected
    assert lib.entry is not None and lib.entry.id == second.id and lib.export_action.text() == "Export Entry…"
    lib.filter.clear()
    assert lib.entry is None and lib.placeholder.text() == "2 entries selected"


def test_several_photos_are_switched_off_moved_and_removed_together(ready, qtbot, monkeypatch):
    asked = []
    monkeypatch.setattr(library_page, "confirm", lambda *a, **k: asked.append(a[2]) or True)
    svc = ready.service
    lib = open_entry(ready)
    grid, entry_id, ids = lib.photos, lib.entry.id, lib.photos.ids()
    qtbot.waitUntil(lambda: grid.visualItemRect(grid.item(2)).width() > 0, timeout=5000)
    click_row(qtbot, grid, 0)
    click_row(qtbot, grid, 2, CMD)
    chosen = [ids[0], ids[2]]

    def faces():
        return [svc.get_entry(entry_id).photo(pid).face for pid in chosen]

    def selected():
        return [i.data(library_page.ROLE_ID) for i in common.selected_items(grid)]

    lib.exclude_btn.click()                                                # both stop being used
    qtbot.waitUntil(lambda: faces() == [None, None], timeout=10000)
    idle(ready, qtbot)
    assert ready.undo_action.text() == "Undo Stop Using 2 Photos" and not any(p in svc.index for p in chosen)
    assert selected() == chosen and lib.exclude_btn.isChecked()           # so the button turns them on again
    lib.exclude_btn.click()
    qtbot.waitUntil(lambda: None not in faces(), timeout=10000)
    idle(ready, qtbot)
    assert ready.undo_action.text() == "Undo Use 2 Photos" and all(p in svc.index for p in chosen)
    ready.undo_action.trigger()
    assert faces() == [None, None]
    ready.undo_action.trigger()
    assert None not in faces()

    grid.setFocus()
    qtbot.keyClick(grid, Qt.Key.Key_Backspace)                             # ⌫: both to the Trash, one question
    assert len(asked) == 1 and "2 photos" in asked[0]
    assert grid.ids() == [p for p in ids if p not in chosen]
    ready.undo_action.trigger()
    assert grid.ids() == ids                                               # back in their old places


def test_several_search_records_are_copied_and_deleted_at_once(ready, qtbot, people, monkeypatch):
    from PySide6.QtGui import QGuiApplication

    from facedict.gui import history_page
    asked = []
    monkeypatch.setattr(history_page, "confirm", lambda *a, **k: asked.append(a[2]) or True)
    for i in range(3):
        run_search(ready, qtbot, held_out(people, i)[1])
    ready.nav.setCurrentRow(1)
    h = ready.history
    qtbot.waitUntil(lambda: h.list.count() == 3, timeout=5000)
    click_row(qtbot, h.list, 0)
    click_row(qtbot, h.list, 1, CMD)
    assert h.several.text() == "2 searches selected" and h.detail.isHidden() and h.delete_btn.isHidden()
    assert h.trash_action.text() == "Move 2 Records to Trash"
    QGuiApplication.clipboard().clear()
    next(a for a in h.list.actions() if a.text() == "Copy").trigger()     # ⌘C: one line per row
    rows = [h.list.item(i).text().replace("\n", " — ") for i in (0, 1)]
    assert QGuiApplication.clipboard().text().splitlines() == rows

    h.list.setFocus()
    qtbot.keyClick(h.list, Qt.Key.Key_Backspace)
    assert len(asked) == 1 and "2 search records" in asked[0]
    assert len(ready.service.list_history()) == 1 and h.list.count() == 1
    assert [i.kind for i in ready.service.list_trash()] == ["history", "history"]
    click_row(qtbot, h.list, 0)
    assert h.several.isHidden() and not h.detail.isHidden() and not h.delete_btn.isHidden()


def test_esc_keeps_the_old_text_and_a_click_elsewhere_saves(ready, qtbot):
    lib = open_entry(ready)
    entry_id, name = lib.entry.id, lib.entry.name
    type_inline(qtbot, lib.title, "Somebody else", finish="esc")
    assert ready.service.get_entry(entry_id).name == name and lib.title.label.text() == name
    open_inline(qtbot, lib.title)                                       # an entry needs a name:
    lib.title.editor.setText("")
    qtbot.keyClick(lib.title.editor, Qt.Key.Key_Return)
    assert lib.title.editing                                            # Enter keeps the box open
    qtbot.keyClick(lib.title.editor, Qt.Key.Key_Escape)
    assert not lib.title.editing and ready.service.get_entry(entry_id).name == name
    type_inline(qtbot, lib.tags_text, "friend，tennis", finish="click")
    assert ready.service.get_entry(entry_id).tags == ["friend", "tennis"]
    assert lib.tags_text.label.text() == "friend  ·  tennis"
    item = next(lib.list.item(i) for i in range(lib.list.count()) if lib.list.item(i).data(library_page.ROLE_ID)
                == entry_id)
    type_inline(qtbot, lib.title, name + " Jr.")
    assert item.text().startswith(name + " Jr.")                       # the list follows at once


def test_drag_maths_inserts_and_swaps():
    from facedict.gui.library_page import PhotoGrid
    ids = ["a", "b", "c", "d"]
    assert PhotoGrid.new_order(ids, [3], "insert", 1) == ["a", "d", "b", "c"]    # move the last one forward
    assert PhotoGrid.new_order(ids, [0], "insert", 3) == ["b", "c", "a", "d"]    # and one backwards
    assert PhotoGrid.new_order(ids, [0], "insert", 4) == ["b", "c", "d", "a"]    # to the very end
    assert PhotoGrid.new_order(ids, [0], "swap", 2) == ["c", "b", "a", "d"]      # swap two photos
    assert PhotoGrid.new_order(ids, [1], "swap", 1) == ids
    # several photos move as a block, in their own order, and are only inserted
    assert PhotoGrid.new_order(ids, [3, 1], "insert", 0) == ["b", "d", "a", "c"]
    assert PhotoGrid.new_order(ids, [0, 2], "insert", 4) == ["b", "d", "a", "c"]
    assert PhotoGrid.new_order(ids, [0, 3], "insert", 2) == ["b", "a", "d", "c"]
    assert PhotoGrid.new_order(ids, [1, 2], "insert", 2) == ids                   # dropped onto themselves
    assert PhotoGrid.new_order(ids, [0, 1], "swap", 3) == ["c", "a", "b", "d"]    # no swap: an insert


def test_merged_order_keeps_photos_added_or_removed_since():
    from facedict.gui.undo import merged_order
    assert merged_order(["a", "b", "c"], ["c", "a", "b"]) == ["c", "a", "b"]
    assert merged_order(["a", "b", "c", "d"], ["c", "a", "b"]) == ["c", "a", "b", "d"]    # d is new
    assert merged_order(["a", "c"], ["c", "b", "a"]) == ["c", "a"]                      # b is gone


def test_dropping_on_a_photo_swaps_between_photos_inserts(ready, qtbot):
    lib = open_entry(ready)
    grid = lib.photos
    assert grid.dragEnabled()                                   # no edit mode to switch on first
    qtbot.waitUntil(lambda: grid.count() > 2 and grid.visualRect(grid.indexFromItem(grid.item(1))).width() > 0,
                    timeout=5000)
    rect = grid.visualRect(grid.indexFromItem(grid.item(1)))
    assert grid.target_at(rect.center()) == ("swap", 1)                          # middle of a photo
    assert grid.target_at(rect.topLeft() + QPoint(2, 10)) == ("insert", 1)       # its left edge
    assert grid.target_at(rect.topRight() + QPoint(-2, 10)) == ("insert", 2)     # its right edge
    for frac, row in ((0.4, 1), (0.6, 2)):                                    # several photos are only inserted
        assert grid.target_at(QPoint(rect.left() + round(rect.width() * frac), rect.center().y()), swap=False) == \
            ("insert", row)
    far = QPoint(grid.viewport().width() - 4, rect.center().y())
    assert grid.target_at(far)[0] == "insert"


def test_reordering_photos_is_saved_at_once_and_undone_with_cmd_z(ready, qtbot):
    from facedict.gui.library_page import PhotoGrid
    lib = open_entry(ready)
    entry_id = lib.entry.id
    ids = [p.photo_id for p in lib.entry.photos]
    assert all(lib.photos.item(i).text() == "" for i in range(lib.photos.count()))     # no captions
    assert lib.photos.item(0).data(library_page.ROLE_COVER) and not lib.photos.item(1).data(library_page.ROLE_COVER)

    lib.photos.reordered.emit(PhotoGrid.new_order(ids, [2], "insert", 1))
    assert [p.photo_id for p in ready.service.get_entry(entry_id).photos][:3] == [ids[0], ids[2], ids[1]]
    lib.photos.reordered.emit(PhotoGrid.new_order(lib.photos.ids(), [0], "swap", len(ids) - 1))
    saved = [p.photo_id for p in ready.service.get_entry(entry_id).photos]
    assert saved[0] == ids[-1] and saved[-1] == ids[0]                          # a new cover, right away
    assert lib.photos.ids() == saved

    assert ready.undo_action.text() == "Undo Reorder Photos"
    ready.undo_action.trigger()
    ready.undo_action.trigger()
    assert [p.photo_id for p in ready.service.get_entry(entry_id).photos] == ids
    assert lib.photos.ids() == ids and not ready.undo_action.isEnabled()
    ready.redo_action.trigger()
    assert [p.photo_id for p in ready.service.get_entry(entry_id).photos][:3] == [ids[0], ids[2], ids[1]]


def test_new_photos_show_up_at_once_and_undo_moves_them_to_the_trash(ready, qtbot, people):
    lib = open_entry(ready)
    name = lib.entry.name.replace(" ", "_")
    before = lib.photos.count()
    lib.add_photo_files(lib.entry.id, [people[name][-1]])                # the held-out photo
    qtbot.waitUntil(lambda: lib.photos.count() == before + 1, timeout=20000)
    assert lib.photos_title.text() == f"Photos ({before + 1})"
    idle(ready, qtbot)
    assert ready.undo_action.text() == "Undo Add Photo"
    ready.undo_action.trigger()
    assert lib.photos.count() == before
    assert [i.kind for i in ready.service.list_trash()] == ["photo"]      # never deleted for good
    ready.redo_action.trigger()
    assert lib.photos.count() == before + 1 and ready.service.list_trash() == []


def test_a_new_entry_shows_its_cover_at_once(ready, qtbot, people, monkeypatch):
    monkeypatch.setattr(library_page.QInputDialog, "getText", lambda *a, **k: ("Someone New", True))
    lib = open_entry(ready)
    lib.new_entry()
    assert lib.entry.name == "Someone New" and lib.photos.count() == 0
    lib.add_photo_files(lib.entry.id, [list(people.values())[0][-1]])
    qtbot.waitUntil(lambda: lib.photos.count() == 1, timeout=20000)
    item = lib.list.currentItem()
    assert item.text().startswith("Someone New") and "1 photo" in item.text()


def test_undo_steps_back_through_every_kind_of_change(ready, qtbot, monkeypatch):
    """⌘Z again and again walks back through the changes, across entries; ⇧⌘Z walks forward; a new
    change after an undo drops the steps that were undone."""
    monkeypatch.setattr(library_page, "confirm", lambda *a, **k: True)
    monkeypatch.setattr(library_page.QInputDialog, "getText", lambda *a, **k: ("Fresh Person", True))
    svc = ready.service
    lib = open_entry(ready, 0)
    first, first_name = lib.entry.id, lib.entry.name
    n_entries = len(svc.list_entries())
    type_inline(qtbot, lib.title, "Renamed")                                      # 1 text
    lib.photos.setCurrentRow(1)
    removed = lib._current_photo().photo_id
    lib.remove_photo()                                                            # 2 photo to the Trash
    lib.new_entry()                                                               # 3 a new entry
    fresh = lib.entry.id
    type_inline(qtbot, lib.tags_text, "new")                                      # 4 text of the new entry
    second = next(e.id for e in svc.list_entries() if e.id not in (first, fresh))
    lib.select_entry(second)
    assert lib.entry.id == second
    lib.delete_entry()                                                            # 5 an entry to the Trash
    assert ready.undo_action.text() == "Undo Delete Entry" and lib.undo_stack.count() == 5

    ready.nav.setCurrentRow(0)                                     # undo from another page shows the Library
    ready.undo_action.trigger()
    assert ready.page_stack.currentWidget() is lib and svc.has_entry(second) and lib.entry.id == second
    ready.undo_action.trigger()
    assert svc.get_entry(fresh).tags == [] and lib.entry.id == fresh               # shows the entry it changes
    ready.undo_action.trigger()
    assert not svc.has_entry(fresh) and len(svc.list_entries()) == n_entries
    ready.undo_action.trigger()
    assert svc.get_entry(first).photo(removed) is not None
    assert [p.photo_id for p in svc.get_entry(first).photos][1] == removed        # in its old place
    ready.undo_action.trigger()
    assert svc.get_entry(first).name == first_name and lib.title.text == first_name
    assert not ready.undo_action.isEnabled() and ready.redo_action.text() == "Redo Edit Name"

    for _ in range(3):
        ready.redo_action.trigger()
    assert svc.get_entry(first).name == "Renamed" and svc.has_entry(fresh)
    assert svc.get_entry(first).photo(removed) is None
    type_inline(qtbot, lib.title, "Fresh")                         # a new change: no more redo
    assert not ready.redo_action.isEnabled() and lib.undo_stack.count() == 4


def test_undo_says_so_when_the_trash_was_emptied(ready, qtbot, monkeypatch):
    monkeypatch.setattr(library_page, "confirm", lambda *a, **k: True)
    warnings = []
    monkeypatch.setattr(library_page.QMessageBox, "warning", lambda *a, **k: warnings.append(a[2]))
    lib = open_entry(ready)
    entry_id = lib.entry.id
    lib.delete_entry()
    for item in ready.service.list_trash():
        ready.service.purge(item.id)                               # “Delete permanently” on the Trash page
    ready.undo_action.trigger()
    assert warnings and "no longer in the Trash" in warnings[0]
    assert not ready.service.has_entry(entry_id) and lib.undo_stack.count() == 0


def test_photo_dialog_offers_no_choice_for_a_photo_with_one_face(ready, qtbot):
    from facedict.gui.library_page import PhotoDialog
    entry, ref = next((e, r) for e in ready.service.list_entries() for r in e.photos
                      if r.face_count == 1 and r.face is not None)
    img, faces = ready.service.detect_faces(ref.photo_id)
    dlg = PhotoDialog(ready.service, ready.runner, entry.id, ref, img, faces, ready)
    qtbot.addWidget(dlg)
    assert len(faces) == 1 and not dlg.view.image.interactive


# --- any script, selectable text ----------------------------------------------------------------------

def test_entries_in_any_script(ready, qtbot):
    svc = ready.service
    han = svc.create_entry("张三", aliases=["Zhang San"], tags=["同事"])
    jp = svc.create_entry("山田 太郎", aliases=["やまだ"], tags=["友達"])
    field = svc.add_global_field("備考")
    han.fields[field.id] = "在东京认识的"
    svc.update_entry(han)
    assert svc.find_entry_by_name("张三").id == han.id
    assert [e.id for e in svc.search_entries("张")] == [han.id]
    assert [e.id for e in svc.search_entries("やまだ")] == [jp.id]
    assert [e.id for e in svc.search_entries("东京", scope=field.id)] == [han.id]
    assert [e.id for e in svc.search_entries("ＺＨＡＮＧ")] == [han.id]       # full-width matches half-width
    lib = open_entry(ready)
    lib.refresh()
    lib.select_entry(han.id)
    assert lib.title.text == "张三" and lib._rows[("field", field.id)].value.text == "在东京认识的"
    type_inline(qtbot, lib.tags_text, "同事，北京")       # a Chinese comma separates values too
    assert svc.get_entry(han.id).tags == ["同事", "北京"]


def test_text_can_be_selected_and_copied(ready, qtbot):
    from PySide6.QtGui import QGuiApplication
    lib = open_entry(ready)
    qtbot.wait(100)
    assert lib.title.label.textInteractionFlags() & Qt.TextInteractionFlag.TextSelectableByMouse
    assert ready.stats.textInteractionFlags() & Qt.TextInteractionFlag.TextSelectableByMouse
    QGuiApplication.clipboard().clear()
    copy_action = next(a for a in lib.list.actions() if a.text() == "Copy")
    lib.list.setCurrentRow(0)
    copy_action.trigger()
    assert lib.list.currentItem().text().splitlines()[0] in QGuiApplication.clipboard().text()


# --- light and dark ---------------------------------------------------------------------------------

def test_switching_to_dark_redraws_the_icons_and_restyles(ready, qapp):
    """Style sheets and line icons are resolved once; a switch to dark (here: a dark palette, since the
    offscreen platform has no colour scheme of its own) has to apply them again."""
    from PySide6.QtGui import QColor, QPalette

    def ink(icon) -> int:                                 # lightness of the strokes of an icon
        img = icon.pixmap(24, 24).toImage()
        pixels = [img.pixelColor(x, y) for x in range(img.width()) for y in range(img.height())]
        return max(pixels, key=lambda c: c.alpha()).lightness()

    light = QPalette(qapp.palette())
    dark = QPalette(light)
    for role, color in ((QPalette.ColorRole.Window, "#323232"), (QPalette.ColorRole.Base, "#171717"),
                        (QPalette.ColorRole.WindowText, "#ffffff"), (QPalette.ColorRole.Text, "#ffffff"),
                        (QPalette.ColorRole.ButtonText, "#ffffff")):
        dark.setColor(role, QColor(color))
    button, nav_item = ready.library.delete_btn, ready.nav.item(0)
    assert ink(button.icon()) < 100 and not common.is_dark()
    try:
        qapp.setPalette(dark)
        common.restyle(qapp)
        assert common.is_dark() and common.DARK_STYLE in qapp.styleSheet()
        assert ink(button.icon()) > 200 and ink(nav_item.icon()) > 200
    finally:
        qapp.setPalette(light)
        common.restyle(qapp)
    assert ink(button.icon()) < 100 and common.DARK_STYLE not in qapp.styleSheet()
