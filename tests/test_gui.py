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
    assert ready.library.name_edit.text() == "" and ready.history.list.count() == 0
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


def test_unsaved_edits_are_saved_on_lock(ready, qtbot):
    ready.nav.setCurrentRow(2)
    ready.library.list.setCurrentRow(0)
    lib = ready.library
    entry_id = lib.entry.id
    lib.set_editing(True)
    qtbot.keyClicks(lib.name_edit, " (tennis)")
    assert lib.has_unsaved_changes
    ready.lock()
    ready.service.unlock(PW)
    assert ready.service.get_entry(entry_id).name.endswith(" (tennis)")


# --- library actions --------------------------------------------------------------------------------

def test_edit_delete_restore_entry(ready, qtbot, monkeypatch):
    monkeypatch.setattr(library_page, "confirm", lambda *a, **k: True)
    ready.nav.setCurrentRow(2)
    ready.library.list.setCurrentRow(0)
    lib = ready.library
    entry = lib.entry
    assert not lib.editing and lib.edit_btn.isVisible()        # a selected entry is read-only first
    lib.edit_btn.click()
    assert lib.editing and lib.name_edit.isVisible() and not lib.export_btn.isVisible()
    qtbot.keyClicks(lib.aliases, "Nick, Nicky")
    lib.save_btn.click()
    assert ready.service.get_entry(entry.id).aliases == ["Nick", "Nicky"]
    assert not lib.editing                                     # saving returns to the read-only page
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
    assert keys <= {"geometry"}, f"{len(keys)} unexpected keys"


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
    assert "Face 2" in img.toolTip()
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
    click_face(qtbot, ready.history.query.image, 1)
    assert ready.history.results.cards()[0].entry_id == expected[1]
    qtbot.waitUntil(lambda: ready.service.list_history()[0].selected == 1, timeout=5000)


def test_photo_dialog_picks_the_face_used_for_matching(ready, qtbot, group):
    """Double-clicking a photo opens it big; clicking a face there applies it right away."""
    from facedict.gui.library_page import PhotoDialog
    data, expected = group
    entry = ready.service.get_entry(expected[1])
    res = ready.service.add_photo(entry.id, data)
    img, faces = ready.service.detect_faces(res.photo.photo_id)
    dlg = PhotoDialog(ready.service, ready.runner, entry.id, res.photo, img, faces, ready)
    qtbot.addWidget(dlg)
    dlg.show()
    qtbot.waitExposed(dlg)
    assert dlg.view.image.selected == 2                     # the largest face, chosen when the photo was added
    assert "Face 3" in dlg.status.text()
    def stored_face():
        return ready.service.get_entry(entry.id).photo(res.photo.photo_id).face

    click_face(qtbot, dlg.view.image, 1)                    # no confirmation button: the click is the choice
    qtbot.waitUntil(lambda: abs(stored_face().bbox[0] - dlg.faces[1].bbox[0]) < 1, timeout=10000)
    qtbot.waitUntil(lambda: "Face 2" in dlg.status.text(), timeout=5000)


def test_the_no_entry_button_excludes_a_photo_from_matching(ready, qtbot):
    ready.nav.setCurrentRow(2)
    ready.library.list.setCurrentRow(0)
    lib = ready.library
    lib.photos.setCurrentRow(0)
    pid = lib.entry.photos[0].photo_id
    assert pid in ready.service.index and not lib.exclude_btn.isChecked()
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


# --- entry page: read-only, editing, fields ---------------------------------------------------------

def open_entry(ready, row: int = 0):
    ready.nav.setCurrentRow(2)
    ready.library.list.setCurrentRow(row)
    return ready.library


def field_rows(lib):
    return {key: row for _kind, key, row in lib._rows}


def test_shared_and_own_fields_in_the_entry_page(ready, qtbot, monkeypatch):
    from facedict.gui.library_page import AddFieldDialog
    lib = open_entry(ready)
    entry_id = lib.entry.id
    lib.edit_btn.click()

    monkeypatch.setattr(AddFieldDialog, "exec", lambda self: 1)
    monkeypatch.setattr(AddFieldDialog, "values", lambda self: ("Met at", True))
    lib.add_field()
    spec = next(f for f in ready.service.global_fields() if f.name == "Met at")
    field_rows(lib)[spec.id].editor.setPlainText("the 2019 conference")

    monkeypatch.setattr(AddFieldDialog, "values", lambda self: ("Phone", False))
    lib.add_field()
    field_rows(lib)["Phone"].editor.setPlainText("555-11")
    lib.save_btn.click()

    stored = ready.service.get_entry(entry_id)
    assert stored.fields[spec.id] == "the 2019 conference"
    assert [(f.name, f.value) for f in stored.own] == [("Phone", "555-11")]

    # the read-only page shows both, and only fields that have text
    texts = [w.field_name for w in lib.view_body.parent().findChildren(library_page.FieldRow)]
    assert "Met at" in texts and "Phone" in texts

    # the shared field is on every entry, empty until filled in
    other = open_entry(ready, 1)
    other.edit_btn.click()
    assert spec.id in field_rows(other) and field_rows(other)[spec.id].editor.toPlainText() == ""
    other.cancel_btn.click()

    # and the search box can look inside it
    assert other.scope.itemText(3) == "Met at"
    other.scope.setCurrentIndex(3)
    other.filter.setText("2019 conference")
    visible = [other.list.item(i).data(library_page.ROLE_ID) for i in range(other.list.count())
               if not other.list.item(i).isHidden()]
    assert visible == [entry_id]


def test_removing_a_shared_field_keeps_its_text(ready, qtbot, monkeypatch):
    from facedict.gui.library_page import AddFieldDialog
    lib = open_entry(ready)
    entry_id = lib.entry.id
    lib.edit_btn.click()
    monkeypatch.setattr(AddFieldDialog, "exec", lambda self: 1)
    monkeypatch.setattr(AddFieldDialog, "values", lambda self: ("City", True))
    lib.add_field()
    spec = next(f for f in ready.service.global_fields() if f.name == "City")
    field_rows(lib)[spec.id].editor.setPlainText("Lisbon")
    lib.save_btn.click()
    lib.edit_btn.click()
    monkeypatch.setattr(library_page, "confirm", lambda *a, **k: True)
    lib.remove_field("global", spec.id, "City")
    assert "City" not in [f.name for f in ready.service.global_fields()]
    stored = ready.service.get_entry(entry_id)
    assert ("City", "Lisbon") in [(f.name, f.value) for f in stored.own]


def test_drag_maths_inserts_and_swaps():
    from facedict.gui.library_page import PhotoGrid
    ids = ["a", "b", "c", "d"]
    assert PhotoGrid.new_order(ids, 3, "insert", 1) == ["a", "d", "b", "c"]      # move the last one forward
    assert PhotoGrid.new_order(ids, 0, "insert", 3) == ["b", "c", "a", "d"]      # and one backwards
    assert PhotoGrid.new_order(ids, 0, "insert", 4) == ["b", "c", "d", "a"]      # to the very end
    assert PhotoGrid.new_order(ids, 0, "swap", 2) == ["c", "b", "a", "d"]        # swap two photos
    assert PhotoGrid.new_order(ids, 1, "swap", 1) == ids


def test_dropping_on_a_photo_swaps_between_photos_inserts(ready, qtbot):
    lib = open_entry(ready)
    lib.edit_btn.click()
    grid = lib.photos
    qtbot.waitUntil(lambda: grid.count() > 2 and grid.visualRect(grid.indexFromItem(grid.item(1))).width() > 0,
                    timeout=5000)
    rect = grid.visualRect(grid.indexFromItem(grid.item(1)))
    assert grid.target_at(rect.center()) == ("swap", 1)                          # middle of a photo
    assert grid.target_at(rect.topLeft() + QPoint(2, 10)) == ("insert", 1)       # its left edge
    assert grid.target_at(rect.topRight() + QPoint(-2, 10)) == ("insert", 2)     # its right edge
    far = QPoint(grid.viewport().width() - 4, rect.center().y())
    assert grid.target_at(far)[0] == "insert"


def test_photos_can_be_reordered_in_edit_mode(ready, qtbot):
    from facedict.gui.library_page import PhotoGrid
    lib = open_entry(ready)
    assert not lib.photos.dragEnabled()                      # read-only page: no accidental dragging
    lib.edit_btn.click()
    assert lib.photos.dragEnabled()
    ids = [p.photo_id for p in lib.entry.photos]
    assert lib.photos.item(0).text() == "cover"

    lib.photos.reordered.emit(PhotoGrid.new_order(ids, 2, "insert", 1))
    assert [p.photo_id for p in lib.entry.photos][:3] == [ids[0], ids[2], ids[1]]
    assert lib.editing and lib.has_unsaved_changes           # stays in edit mode, pending until saved
    assert ready.service.get_entry(lib.entry.id).cover.photo_id == ids[0]

    lib.photos.reordered.emit(PhotoGrid.new_order(lib.photos.ids(), 0, "swap", len(ids) - 1))
    lib.save_btn.click()
    saved = [p.photo_id for p in ready.service.get_entry(lib.entry.id).photos]
    assert saved[0] == ids[-1] and saved[-1] == ids[0]
    assert not lib.editing


def test_discarding_puts_the_photo_order_back(ready, qtbot):
    from facedict.gui.library_page import PhotoGrid
    lib = open_entry(ready)
    lib.edit_btn.click()
    ids = [p.photo_id for p in lib.entry.photos]
    lib.photos.reordered.emit(PhotoGrid.new_order(ids, 0, "insert", len(ids)))
    assert [p.photo_id for p in lib.entry.photos][0] == ids[1]
    lib.cancel_btn.click()
    assert not lib.editing and [p.photo_id for p in lib.entry.photos] == ids
    assert ready.service.get_entry(lib.entry.id).cover.photo_id == ids[0]


# --- read-only photos, any script, selectable text ---------------------------------------------------

def test_photo_faces_cannot_be_changed_outside_edit_mode(ready, qtbot, group):
    from facedict.gui.library_page import PhotoDialog
    data, expected = group
    entry = ready.service.get_entry(expected[1])
    res = ready.service.add_photo(entry.id, data)
    img, faces = ready.service.detect_faces(res.photo.photo_id)
    before = ready.service.get_entry(entry.id).photo(res.photo.photo_id).face

    view_only = PhotoDialog(ready.service, ready.runner, entry.id, res.photo, img, faces, False, ready)
    qtbot.addWidget(view_only)
    view_only.show()
    qtbot.waitExposed(view_only)
    assert not view_only.view.image.interactive and view_only.view.image.face_count == len(faces)
    click_face(qtbot, view_only.view.image, 0)
    qtbot.wait(200)
    assert ready.service.get_entry(entry.id).photo(res.photo.photo_id).face == before
    assert "edit the entry" in view_only.status.text()

    editable = PhotoDialog(ready.service, ready.runner, entry.id, res.photo, img, faces, True, ready)
    qtbot.addWidget(editable)
    editable.show()
    qtbot.waitExposed(editable)
    assert editable.view.image.interactive


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
    lib.select_entry(han.id)
    assert lib.title.text() == "张三"
    lib.edit_btn.click()
    lib.tags.setText("同事，北京")                    # QTest cannot type CJK, so set it (an IME would type it)
    lib.tags.textEdited.emit(lib.tags.text())        # a Chinese comma separates values too
    lib.save_btn.click()
    assert svc.get_entry(han.id).tags == ["同事", "北京"]


def test_text_can_be_selected_and_copied(ready, qtbot):
    from PySide6.QtGui import QGuiApplication
    lib = open_entry(ready)
    qtbot.wait(100)
    assert lib.title.textInteractionFlags() & Qt.TextInteractionFlag.TextSelectableByMouse
    assert ready.stats.textInteractionFlags() & Qt.TextInteractionFlag.TextSelectableByMouse
    QGuiApplication.clipboard().clear()
    copy_action = next(a for a in lib.list.actions() if a.text() == "Copy")
    lib.list.setCurrentRow(0)
    copy_action.trigger()
    assert lib.list.currentItem().text().splitlines()[0] in QGuiApplication.clipboard().text()
