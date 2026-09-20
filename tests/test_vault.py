"""M1: encrypted vault - lifecycle, storage, tamper detection, locking, trash."""

from __future__ import annotations

import hashlib
import json

import numpy as np
import pytest

from facedict import config
from facedict.crypto import TamperedError, WrongPasswordError
from facedict.models import (Confirmation, Face, FaceSearch, FieldDef, HistoryRecord, Match, OwnField,
                             PhotoRef, new_id, now_iso)
from facedict.vault import (RestoreConflictError, UnsupportedVaultError, Vault, VaultError, VaultExistsError,
                            VaultLockedError, VaultNotFoundError)

PW = "correct horse battery staple"
FAST_KDF = 2 ** 12


@pytest.fixture
def vault(isolated_vault):
    v, _ = Vault.create(isolated_vault, PW, kdf_n=FAST_KDF)
    return v


def snapshot(root) -> dict[str, str]:
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*")) if p.is_file()}


def add_photo_entry(vault: Vault, name: str, n_photos: int = 2):
    entry = vault.new_entry(name, own=[OwnField("note", f"notes about {name}")])
    for i in range(n_photos):
        data = f"photo-{name}-{i}".encode() * 10
        pid = vault.put_photo(data)
        vault.put_thumb(pid, b"thumb" + data[:8])
        entry.photos.append(PhotoRef(photo_id=pid, sha256=hashlib.sha256(data).hexdigest(),
                                     face=Face([1, 2, 3, 4], [[0, 0]] * 5, 0.9)))
    return vault.save_entry(entry)


# --- lifecycle ---------------------------------------------------------------------

def test_create_open_and_wrong_password(isolated_vault):
    v, code = Vault.create(isolated_vault, PW, kdf_n=FAST_KDF, recovery_code=True)
    assert Vault.exists(isolated_vault)
    e = v.new_entry("Alice")
    reopened = Vault.open(isolated_vault, PW)
    assert reopened.get_entry(e.id).name == "Alice"
    with pytest.raises(WrongPasswordError):
        Vault.open(isolated_vault, "wrong")
    assert len(code.replace("-", "")) == 32


def test_recovery_code_resets_password(isolated_vault):
    v, code = Vault.create(isolated_vault, PW, kdf_n=FAST_KDF, recovery_code=True)
    e = v.new_entry("Bob")
    with pytest.raises(WrongPasswordError):
        Vault.recover(isolated_vault, "AAAA-BBBB", "new-pass")
    recovered = Vault.recover(isolated_vault, code.lower(), "new-pass")
    assert recovered.get_entry(e.id).name == "Bob"
    assert Vault.open(isolated_vault, "new-pass").get_entry(e.id).name == "Bob"
    with pytest.raises(WrongPasswordError):
        Vault.open(isolated_vault, PW)
    # the recovery code keeps working after a password reset
    Vault.recover(isolated_vault, code, "third-pass")


def test_change_password(vault, isolated_vault):
    with pytest.raises(WrongPasswordError):
        vault.change_password("nope", "x")
    vault.change_password(PW, "new password")
    Vault.open(isolated_vault, "new password")
    with pytest.raises(WrongPasswordError):
        Vault.open(isolated_vault, PW)


def test_create_refuses_existing_or_nonempty(isolated_vault, tmp_path):
    Vault.create(isolated_vault, PW, kdf_n=FAST_KDF)
    with pytest.raises(VaultExistsError):
        Vault.create(isolated_vault, PW, kdf_n=FAST_KDF)
    other = tmp_path / "other"
    other.mkdir()
    (other / "keep.txt").write_text("user file")
    with pytest.raises(VaultError):
        Vault.create(other, PW, kdf_n=FAST_KDF)
    assert (other / "keep.txt").read_text() == "user file"
    with pytest.raises(VaultNotFoundError):
        Vault.open(tmp_path / "missing", PW)


def test_tests_refuse_the_real_vault():
    with pytest.raises(VaultError):
        Vault.create(config.DEFAULT_VAULT_DIR, PW)
    with pytest.raises(VaultError):
        Vault.open(config.DEFAULT_VAULT_DIR, PW)


def test_permissions_are_private(vault):
    assert (vault.root.stat().st_mode & 0o777) == 0o700
    add_photo_entry(vault, "Carol")
    for p in vault.root.rglob("*"):
        mode = p.stat().st_mode & 0o777
        assert mode == (0o700 if p.is_dir() else 0o600), p


# --- storage -------------------------------------------------------------------------

def test_entries_photos_thumbs_index_roundtrip(vault, isolated_vault):
    entry = add_photo_entry(vault, "Dave", 3)
    entry.aliases = ["D"]
    entry.own = [OwnField("birthday", "1990-01-01")]
    spec = FieldDef(id=new_id(), name="Notes")
    vault.set_field_defs([spec])
    entry.fields = {spec.id: "met in Lisbon"}
    vault.save_entry(entry)
    v2 = Vault.open(isolated_vault, PW)
    got = v2.get_entry(entry.id)
    assert got.aliases == ["D"] and [f.to_dict() for f in got.own] == [{"name": "birthday", "value": "1990-01-01"}]
    assert got.fields == {spec.id: "met in Lisbon"}
    assert [f.name for f in v2.get_field_defs()] == ["Notes"]
    assert len(got.photos) == 3
    assert v2.get_photo(got.photos[0].photo_id).startswith(b"photo-Dave-0")
    assert v2.get_thumb(got.photos[0].photo_id).startswith(b"thumb")
    assert v2.get_thumb(new_id()) is None
    v2.save_index("arcface_mbf", b"index-bytes")
    assert v2.load_index("arcface_mbf") == b"index-bytes"
    assert v2.load_index("sface") is None
    assert [e.name for e in v2.list_entries()] == ["Dave"]


def test_returned_objects_are_copies(vault):
    entry = vault.new_entry("Eve")
    entry.name = "Mallory"                       # not saved
    assert vault.get_entry(entry.id).name == "Eve"


def test_history_roundtrip(vault, isolated_vault):
    pid = vault.put_photo(b"query-bytes")
    face = Face([1, 2, 3, 4], [[0, 0]] * 5, 0.9)
    rec = HistoryRecord(id=new_id(), time=now_iso(), photo_id=pid, model="arcface_mbf", selected=1, searches=[
        FaceSearch(face, np.arange(4, dtype=np.float32), [Match(1, new_id(), "Frank", 0.8, None)]),
        FaceSearch(face, np.ones(4, dtype=np.float32), [Match(1, new_id(), "Grace", 0.7, None)])])
    vault.save_history(rec)
    rec.searches[0].confirmed = Confirmation(entry_id=new_id(), added_photo_id=new_id())
    vault.save_history(rec)
    got = Vault.open(isolated_vault, PW).get_history(rec.id)
    assert got.face_count == 2 and got.selected == 1
    assert got.matches[0].name_snapshot == "Grace"                  # the selected face
    assert got.search(0).matches[0].name_snapshot == "Frank"
    assert np.array_equal(got.search(0).embedding, np.arange(4, dtype=np.float32))
    assert got.search(0).confirmed.entry_id == rec.searches[0].confirmed.entry_id
    assert got.confirmed is None and got.any_confirmed


def test_history_records_of_the_first_version_still_load():
    old = {"id": new_id(), "time": now_iso(), "photo_id": new_id(), "face_count": 1, "model": "arcface_mbf",
           "face": {"bbox": [1, 2, 3, 4], "landmarks": [[0, 0]] * 5, "score": 0.9},
           "embedding": HistoryRecord.from_dict({"id": "x", "time": "t", "photo_id": "p", "searches": [
               {"face": {"bbox": [0, 0, 1, 1], "landmarks": [[0, 0]] * 5}, "embedding": None, "matches": []}]})
           .searches[0].to_dict()["embedding"],
           "matches": [{"rank": 1, "entry_id": "e1", "name_snapshot": "Old", "score": 0.5, "best_photo_id": None}],
           "confirmed": {"entry_id": "e1", "added_photo_id": "p1", "time": now_iso()}}
    rec = HistoryRecord.from_dict(old)
    assert rec.face_count == 1 and rec.selected == 0
    assert rec.face.bbox == [1, 2, 3, 4] and rec.matches[0].name_snapshot == "Old"
    assert rec.confirmed.added_photo_id == "p1"
    assert HistoryRecord.from_dict(rec.to_dict()).to_dict() == rec.to_dict()


def test_invalid_ids_are_rejected(vault):
    with pytest.raises(VaultError):
        vault.get_photo("../../etc/passwd")
    with pytest.raises(VaultError):
        vault.save_index("../evil", b"x")


# --- tamper detection ------------------------------------------------------------------

def test_modified_or_swapped_files_are_detected(vault, isolated_vault):
    entry = add_photo_entry(vault, "Grace", 2)
    assert vault.verify() == []
    p1, p2 = (isolated_vault / "photos" / f"{p.photo_id}.enc" for p in entry.photos)
    b1, b2 = p1.read_bytes(), p2.read_bytes()
    p1.write_bytes(b2)                                    # swap the two photos
    p2.write_bytes(b1)
    with pytest.raises(TamperedError):
        vault.get_photo(entry.photos[0].photo_id)
    p1.write_bytes(b1)
    p2.write_bytes(b2)
    efile = isolated_vault / "entries" / f"{entry.id}.enc"
    data = bytearray(efile.read_bytes())
    data[-5] ^= 0xFF
    efile.write_bytes(bytes(data))
    problems = vault.verify()
    assert len(problems) == 1 and entry.id in problems[0]


# --- locking -----------------------------------------------------------------------------

def test_lock_clears_memory_and_never_touches_disk(vault, isolated_vault):
    entry = add_photo_entry(vault, "Heidi", 2)
    vault.save_index("arcface_mbf", b"idx")
    before = snapshot(isolated_vault)
    vault.lock()
    assert vault.locked
    assert snapshot(isolated_vault) == before
    for call in (vault.list_entries, vault.list_history, vault.list_trash,
                 lambda: vault.get_photo(entry.photos[0].photo_id),
                 lambda: vault.get_thumb(entry.photos[0].photo_id),
                 lambda: vault.load_index("arcface_mbf"),
                 lambda: vault.put_photo(b"x")):
        with pytest.raises(VaultLockedError):
            call()
    assert snapshot(isolated_vault) == before
    assert Vault.open(isolated_vault, PW).get_entry(entry.id).name == "Heidi"


# --- trash -------------------------------------------------------------------------------

def test_delete_entry_goes_to_trash_and_restores(vault, isolated_vault):
    keep = add_photo_entry(vault, "Ivan", 1)
    gone = add_photo_entry(vault, "Judy", 2)
    before = snapshot(isolated_vault)
    item = vault.delete_entry(gone.id)
    assert not vault.has_entry(gone.id)
    assert [e.id for e in vault.list_entries()] == [keep.id]
    assert len(item.files) == 5                          # entry + 2 photos + 2 thumbs
    listed = vault.list_trash()
    assert [(i.kind, i.label) for i in listed] == [("entry", "Judy")]
    # data still exists (as ciphertext) inside the trash folder
    assert len(list((isolated_vault / "trash" / item.id).rglob("*.enc"))) == 6
    vault.restore(item.id)
    assert vault.get_entry(gone.id).name == "Judy"
    assert vault.list_trash() == []
    assert snapshot(isolated_vault) == before
    assert vault.verify() == []


def test_remove_photo_and_restore(vault):
    entry = add_photo_entry(vault, "Ken", 2)
    pid = entry.photos[0].photo_id
    item = vault.remove_photo(entry.id, pid)
    assert [p.photo_id for p in vault.get_entry(entry.id).photos] == [entry.photos[1].photo_id]
    assert not vault.has_photo(pid)
    vault.restore(item.id)
    assert {p.photo_id for p in vault.get_entry(entry.id).photos} == {p.photo_id for p in entry.photos}
    assert vault.get_photo(pid).startswith(b"photo-Ken-0")


def test_photo_restore_requires_its_entry(vault):
    entry = add_photo_entry(vault, "Leo", 2)
    photo_item = vault.remove_photo(entry.id, entry.photos[0].photo_id)
    entry_item = vault.delete_entry(entry.id)
    with pytest.raises(RestoreConflictError):
        vault.restore(photo_item.id)
    vault.restore(entry_item.id)
    vault.restore(photo_item.id)
    assert len(vault.get_entry(entry.id).photos) == 2


def test_delete_history_and_purge(vault, isolated_vault):
    pid = vault.put_photo(b"q")
    rec = vault.save_history(HistoryRecord(id=new_id(), time=now_iso(), photo_id=pid, model="m", searches=[]))
    item = vault.delete_history(rec.id)
    assert vault.list_history() == []
    vault.purge(item.id)
    assert not (isolated_vault / "trash" / item.id).exists()
    assert vault.list_trash() == []
    with pytest.raises(VaultError):
        vault.purge(new_id())


def test_trash_survives_reopen(vault, isolated_vault):
    entry = add_photo_entry(vault, "Mia", 1)
    vault.delete_entry(entry.id)
    v2 = Vault.open(isolated_vault, PW)
    assert [i.label for i in v2.list_trash()] == ["Mia"]


def test_new_recovery_code_replaces_the_old_one(vault, isolated_vault):
    with pytest.raises(WrongPasswordError):
        vault.new_recovery_code("wrong")
    code1 = vault.new_recovery_code(PW)
    code2 = vault.new_recovery_code(PW)
    assert code1 != code2
    with pytest.raises(WrongPasswordError):
        Vault.recover(isolated_vault, code1, "x-new-pass")
    Vault.recover(isolated_vault, code2, "x-new-pass")


def test_damaged_entry_is_skipped_not_fatal(vault, isolated_vault):
    good = add_photo_entry(vault, "Nora", 1)
    bad = add_photo_entry(vault, "Oscar", 1)
    efile = isolated_vault / "entries" / f"{bad.id}.enc"
    data = bytearray(efile.read_bytes())
    data[-3] ^= 0x55
    efile.write_bytes(bytes(data))
    v2 = Vault.open(isolated_vault, PW)
    assert [e.name for e in v2.list_entries()] == ["Nora"]
    assert v2.unreadable == {f"entries/{bad.id}.enc"}
    assert v2.get_entry(good.id).name == "Nora"


# --- database format, recovery methods ---------------------------------------------------------

QUESTIONS = ["First pet?", "First school?", "Favourite dish?"]
ANSWERS = ["Mimi", "St. Mary's", "\u62c9\u9762"]


def test_inspect_tells_what_a_folder_is(isolated_vault, tmp_path):
    info = Vault.inspect(isolated_vault)
    assert info.can_create and not info.is_vault and info.error is None
    Vault.create(isolated_vault, PW, kdf_n=FAST_KDF)
    info = Vault.inspect(isolated_vault)
    assert info.is_vault and info.usable and info.format == 2 and info.recovery_methods == []
    other = tmp_path / "not-a-db"
    other.mkdir()
    (other / "holiday.jpg").write_bytes(b"x")
    assert Vault.inspect(other).error and not Vault.inspect(other).can_create
    missing = Vault.inspect(tmp_path / "nowhere")
    assert missing.can_create and missing.error is None


def test_inspect_rejects_a_newer_format(isolated_vault):
    Vault.create(isolated_vault, PW, kdf_n=FAST_KDF)
    keyfile = isolated_vault / "vault.json"
    doc = json.loads(keyfile.read_text())
    doc["format"] = 99
    keyfile.write_text(json.dumps(doc))
    info = Vault.inspect(isolated_vault)
    assert info.is_vault and not info.usable and "newer version" in info.error
    with pytest.raises(UnsupportedVaultError):
        Vault.open(isolated_vault, PW)


def test_security_questions_two_of_three(isolated_vault):
    v, _ = Vault.create(isolated_vault, PW, kdf_n=FAST_KDF)
    with pytest.raises(WrongPasswordError):
        v.set_questions("wrong", QUESTIONS, ANSWERS)
    v.set_questions(PW, QUESTIONS, ANSWERS, required=2)
    info = Vault.inspect(isolated_vault)
    assert info.questions == QUESTIONS and info.required == 2 and info.recovery_methods == ["questions"]
    # any two answers, forgiving about case, spaces and punctuation
    back = Vault.recover_with_answers(isolated_vault, {0: " mimi ", 2: ANSWERS[2]}, "brand new password")
    assert back.stats()["entries"] == 0
    assert Vault.open(isolated_vault, "brand new password")
    Vault.recover_with_answers(isolated_vault, {1: "st marys", 2: ANSWERS[2]}, "third password")
    with pytest.raises(WrongPasswordError):
        Vault.recover_with_answers(isolated_vault, {0: "Mimi"}, "x password")        # only one answer
    with pytest.raises(WrongPasswordError):
        Vault.recover_with_answers(isolated_vault, {0: "Mimi", 1: "wrong"}, "x password")


def test_security_questions_all_required_and_validation(isolated_vault):
    v, _ = Vault.create(isolated_vault, PW, kdf_n=FAST_KDF)
    v.set_questions(PW, QUESTIONS, ANSWERS, required=3)
    assert Vault.inspect(isolated_vault).required == 3
    with pytest.raises(WrongPasswordError):
        Vault.recover_with_answers(isolated_vault, {0: ANSWERS[0], 1: ANSWERS[1]}, "x password")
    v2 = Vault.recover_with_answers(isolated_vault, dict(enumerate(ANSWERS)), "all three password")
    for bad in ([""], ["just one question?"], ["q"] * 7):        # 0/1 questions, or too many
        with pytest.raises(ValueError):
            v2.set_questions("all three password", bad, ["answer"] * len(bad))
    with pytest.raises(ValueError):
        v2.set_questions("all three password", QUESTIONS, ["ok", "ok", " "])      # empty answer
    with pytest.raises(ValueError):
        v2.set_questions("all three password", ["same", "same", "x"], ANSWERS)    # duplicate questions


def test_both_recovery_methods_and_removal(isolated_vault):
    v, _ = Vault.create(isolated_vault, PW, kdf_n=FAST_KDF)
    v.set_questions(PW, QUESTIONS, ANSWERS)
    code = v.new_recovery_code(PW)
    info = Vault.inspect(isolated_vault)
    assert info.recovery_methods == ["questions", "code"]
    v.clear_recovery(PW, "questions")
    assert Vault.inspect(isolated_vault).recovery_methods == ["code"]
    Vault.recover(isolated_vault, code, "code password")
    with pytest.raises(VaultError):
        Vault.recover_with_answers(isolated_vault, {0: ANSWERS[0], 1: ANSWERS[1]}, "y password")


def test_a_format_1_database_still_opens_and_upgrades(isolated_vault):
    """Vaults written by the first version have a single recovery code and no slots."""
    v, code = Vault.create(isolated_vault, PW, kdf_n=FAST_KDF, recovery_code=True)
    keyfile = isolated_vault / "vault.json"
    doc = json.loads(keyfile.read_text())
    slot = doc["recovery"]["slots"][0]
    doc["recovery"] = {"salt": slot["salt"], "wrapped_key": slot["wrapped_key"]}    # old shape
    doc["format"] = 1
    keyfile.write_text(json.dumps(doc))
    info = Vault.inspect(isolated_vault)
    assert info.usable and info.format == 1 and info.recovery_methods == ["code"]
    opened = Vault.open(isolated_vault, PW)
    opened.new_entry("Someone")
    Vault.recover(isolated_vault, code, "upgraded password")                        # old code still works
    assert json.loads(keyfile.read_text())["format"] == 2                           # rewritten in the new shape
    assert Vault.open(isolated_vault, "upgraded password").list_entries()[0].name == "Someone"
