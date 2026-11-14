"""M2: application service end to end on public LFW data (temp vault only)."""

from __future__ import annotations

import hashlib
import json
import shutil

import numpy as np
import pytest

from facedict.models import OwnField, new_id, now_iso
from facedict.service import FaceDictionaryService, ServiceError
from facedict.vault import VaultLockedError
from tests.conftest import SAMPLES, lfw_people

PW = "service-test-password"
N_PEOPLE = 8


@pytest.fixture(scope="module")
def people():
    ppl = lfw_people(min_images=5)
    return dict(list(ppl.items())[:N_PEOPLE])


@pytest.fixture
def library_dir(tmp_path, people):
    """Import folder: <person>/ with all but the last photo; the last one is held out for queries."""
    root = tmp_path / "import"
    for name, imgs in people.items():
        d = root / name
        d.mkdir(parents=True)
        for p in imgs[:-1]:
            shutil.copy(p, d / p.name)
    first = next(iter(people))
    (root / first / "entry.json").write_text(json.dumps(
        {"name": first.replace("_", " "), "aliases": ["Alias One"], "tags": ["test"], "notes": "hello",
         "birthday": "1970-01-01"}), "utf-8")
    return root


@pytest.fixture
def svc(isolated_vault, models_available, library_dir):
    s = FaceDictionaryService(isolated_vault)
    s.create_vault(PW, kdf_n=2 ** 12)
    report = s.import_folder(library_dir)
    assert report.entries_created == N_PEOPLE and not report.errors
    return s


def held_out(people, i=0) -> tuple[str, bytes]:
    name = list(people)[i]
    return name.replace("_", " "), people[name][-1].read_bytes()


def tree_hash(root) -> dict:
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*")) if p.is_file()}


def test_import_reads_metadata_and_leaves_sources_untouched(isolated_vault, models_available, library_dir, people):
    before = tree_hash(library_dir)
    s = FaceDictionaryService(isolated_vault)
    s.create_vault(PW, kdf_n=2 ** 12)
    report = s.import_folder(library_dir)
    assert tree_hash(library_dir) == before
    assert report.photos_added == sum(len(v) - 1 for v in people.values())
    first = s.find_entry_by_name(next(iter(people)).replace("_", " "))
    assert first.aliases == ["Alias One"] and first.tags == ["test"]
    notes = next(f for f in s.global_fields() if f.name == "Notes")     # "notes" becomes a shared field
    assert first.fields[notes.id] == "hello"
    assert [(f.name, f.value) for f in first.own] == [("birthday", "1970-01-01")]
    assert s.stats()["indexed"] == report.photos_added - report.no_face
    again = s.import_folder(library_dir)                # re-import: everything is a duplicate
    assert again.entries_created == 0 and again.photos_added == 0
    assert again.duplicates == report.photos_added


def test_query_finds_the_right_person_and_saves_history(svc, people):
    hits = 0
    for i in range(N_PEOPLE):
        name, data = held_out(people, i)
        res = svc.query(data)
        assert res.face is not None and len(res.matches) == 5
        assert [m.rank for m in res.matches] == [1, 2, 3, 4, 5]
        assert res.matches[0].score >= res.matches[-1].score
        hits += res.matches[0].name_snapshot == name
        rec = svc.get_history(res.history.id)
        assert [m.entry_id for m in rec.matches] == [m.entry_id for m in res.matches]
        assert rec.embedding is not None and rec.model == "arcface_mbf"
    assert hits >= N_PEOPLE - 1
    assert len(svc.list_history()) == N_PEOPLE


def test_query_without_face_saves_nothing(svc):
    res = svc.query((SAMPLES / "no_face.jpg").read_bytes())
    assert res.face is None and res.matches == [] and res.history is None
    assert svc.list_history() == []


def test_confirm_adds_query_photo_without_recomputing(svc, people, monkeypatch):
    name, data = held_out(people, 1)
    res = svc.query(data)
    entry_id = res.matches[0].entry_id
    n_before = len(svc.get_entry(entry_id).photos)
    eng = svc.engine()
    monkeypatch.setattr(eng, "embed", lambda *a, **k: pytest.fail("embedding was recomputed"))
    ref = svc.confirm(res.history.id, entry_id)
    monkeypatch.undo()
    entry = svc.get_entry(entry_id)
    assert len(entry.photos) == n_before + 1 and ref.from_query == res.history.id and ref.query_face == 0
    assert svc.photo_bytes(ref.photo_id) == data
    assert np.allclose(svc.index.vector(ref.photo_id), res.history.embedding)
    assert svc.get_history(res.history.id).confirmed.added_photo_id == ref.photo_id
    # confirming twice does not duplicate the photo
    svc.confirm(res.history.id, entry_id)
    assert len(svc.get_entry(entry_id).photos) == n_before + 1
    # the same person's photo now matches itself perfectly
    again = svc.query(data)
    assert again.matches[0].entry_id == entry_id and again.matches[0].score > 0.99


def test_unconfirm_and_restore_keep_history_consistent(svc, people):
    _, data = held_out(people, 2)
    res = svc.query(data)
    entry_id = res.matches[0].entry_id
    ref = svc.confirm(res.history.id, entry_id)
    item = svc.unconfirm(res.history.id)
    assert item is not None and item.kind == "photo"
    assert svc.get_entry(entry_id).photo(ref.photo_id) is None
    assert ref.photo_id not in svc.index
    assert svc.get_history(res.history.id).confirmed is None
    svc.restore(item.id)
    assert svc.get_entry(entry_id).photo(ref.photo_id) is not None
    assert ref.photo_id in svc.index
    assert svc.get_history(res.history.id).confirmed.added_photo_id == ref.photo_id


def test_history_and_album_are_independent(svc, people):
    _, data = held_out(people, 3)
    res = svc.query(data)
    ref = svc.confirm(res.history.id, res.matches[0].entry_id)
    svc.delete_history(res.history.id)
    assert svc.photo_bytes(ref.photo_id) == data            # album copy survives
    assert svc.list_history() == []


def test_delete_and_restore_entry_updates_index(svc, people):
    name, data = held_out(people, 4)
    top = svc.query(data, save_history=False).matches[0]
    item = svc.delete_entry(top.entry_id)
    assert top.entry_id not in svc.index.entry_ids
    assert all(m.entry_id != top.entry_id for m in svc.query(data, save_history=False).matches)
    svc.restore(item.id)
    assert svc.query(data, save_history=False).matches[0].entry_id == top.entry_id


def test_lock_unlock_reuses_saved_index(svc, isolated_vault, people):
    n = len(svc.index)
    svc.lock()
    with pytest.raises(VaultLockedError):
        svc.list_entries()
    with pytest.raises(VaultLockedError):
        svc.query(held_out(people)[1])
    svc.unlock(PW)
    stats = svc.ensure_index()
    assert stats["computed"] == 0 and stats["total"] == n


def test_switch_recognizer_and_back(svc, people):
    name, data = held_out(people, 0)
    stats = svc.set_recognizer("sface")
    assert stats["computed"] > 0 and svc.index.dim == 128
    assert svc.query(data, save_history=False).matches[0].name_snapshot == name
    back = svc.set_recognizer("arcface_mbf")
    assert back["computed"] == 0 and svc.index.dim == 512       # the old index was kept in the vault
    svc.lock()
    svc.unlock(PW)
    assert svc.recognizer == "arcface_mbf"


def test_multi_face_photo_and_choose_face(svc):
    entry = svc.create_entry("Group Photo Person")
    res = svc.add_photo(entry.id, (SAMPLES / "two_faces.jpg").read_bytes())
    assert res.face_count == 2 and res.photo.face is not None
    img, faces = svc.detect_faces(res.photo.photo_id)
    assert len(faces) == 2
    v_before = svc.index.vector(res.photo.photo_id)
    svc.choose_face(entry.id, res.photo.photo_id, faces[1])
    assert not np.allclose(svc.index.vector(res.photo.photo_id), v_before)
    svc.choose_face(entry.id, res.photo.photo_id, None)
    assert res.photo.photo_id not in svc.index


def test_add_photo_edge_cases(svc):
    entry = svc.create_entry("Edge Cases")
    assert svc.add_photo(entry.id, b"garbage").error
    no_face = svc.add_photo(entry.id, (SAMPLES / "no_face.jpg").read_bytes())
    assert no_face.face_count == 0 and no_face.photo.photo_id not in svc.index
    heic = svc.add_photo(entry.id, (SAMPLES / "photo.heic").read_bytes())
    assert heic.face_count == 1 and heic.photo.photo_id in svc.index
    assert svc.thumb_bytes(heic.photo.photo_id)[:3] == b"\xff\xd8\xff"


def test_export_writes_plaintext_copy(svc, tmp_path):
    entry = svc.list_entries()[0]
    out = svc.export_entry(entry.id, tmp_path / "export")
    doc = json.loads((out / "entry.json").read_text("utf-8"))
    assert doc["name"] == entry.name
    assert len(list(out.glob("photo_*.jpg"))) == len(entry.photos)
    out2 = svc.export_entry(entry.id, tmp_path / "export")
    assert out2 != out and out2.name.endswith("(2)")


def test_update_entry_cannot_clobber_photos(svc):
    entry = svc.list_entries()[0]
    n = len(entry.photos)
    entry.name = "Renamed"
    entry.photos = []
    svc.update_entry(entry)
    got = svc.get_entry(entry.id)
    assert got.name == "Renamed" and len(got.photos) == n


def test_vault_integrity_after_everything(svc, people):
    _, data = held_out(people, 5)
    res = svc.query(data)
    svc.confirm(res.history.id, res.matches[0].entry_id)
    svc.delete_entry(svc.list_entries()[-1].id)
    assert svc.vault.verify() == []


def test_only_one_instance_may_open_the_vault(svc, isolated_vault):
    from facedict.service import VaultInUseError
    other = FaceDictionaryService(isolated_vault)
    with pytest.raises(VaultInUseError):
        other.unlock(PW)
    assert not other.unlocked
    svc.lock()
    other.unlock(PW)
    assert other.unlocked
    with pytest.raises(VaultInUseError):
        svc.unlock(PW)
    other.lock()


# --- several faces in one photo -------------------------------------------------------------------

@pytest.fixture
def group(svc, people):
    """A group photo of four library people (held-out photos), left to right; the second is largest."""
    from tests.conftest import group_photo, single_face_photos
    names = single_face_photos(people, 4)
    data = group_photo([people[n][-1] for n in names], [150, 260, 190, 120])
    return data, [svc.find_entry_by_name(n.replace("_", " ")).id for n in names]


def test_every_face_of_a_group_photo_is_searched(svc, group):
    data, expected = group
    res = svc.query(data)
    assert len(res.searches) == 4
    assert [s.matches[0].entry_id for s in res.searches] == expected      # reading order = left to right
    assert res.selected == 1 and res.face is res.searches[1].face          # the largest face is shown first
    xs = [s.face.bbox[0] for s in res.searches]
    assert xs == sorted(xs)
    rec = svc.get_history(res.history.id)
    assert rec.face_count == 4 and rec.selected == 1
    assert [s.matches[0].entry_id for s in rec.searches] == expected


def thumb_face(svc, rec) -> int:
    """Which face of a search record its thumbnail shows."""
    from facedict.engine import thumbnail
    from facedict.imageio import decode
    img = decode(svc._v().get_photo(rec.photo_id))
    thumb = decode(svc.thumb_bytes(rec.photo_id)).astype(float)
    return int(np.argmin([np.abs(thumbnail(img, s.face).astype(float) - thumb).mean() for s in rec.searches]))


def test_history_thumbnail_follows_the_face_picked(svc, group):
    data, _ = group
    rec = svc.query(data).history
    assert thumb_face(svc, rec) == 1                       # the largest face, shown first
    svc.set_selected(rec.id, 3)
    assert thumb_face(svc, svc.get_history(rec.id)) == 3
    # a thumbnail left behind by an older version is made again
    svc._store_thumb(rec.photo_id, svc.photo_image(rec.photo_id), rec.searches[1].face)
    assert svc.remake_history_thumbs([rec.id]) == [rec.id]
    assert thumb_face(svc, svc.get_history(rec.id)) == 3


def test_confirm_and_undo_per_face(svc, group, monkeypatch):
    data, expected = group
    rec = svc.query(data).history
    monkeypatch.setattr(svc.engine(), "embed", lambda *a, **k: pytest.fail("recomputed"))
    ref2 = svc.confirm(rec.id, expected[2], face_index=2)
    ref0 = svc.confirm(rec.id, expected[0], face_index=0)
    monkeypatch.undo()
    assert ref2.source == f"query:{rec.id}#2" and ref2.face == rec.searches[2].face
    assert ref0.face == rec.searches[0].face
    got = svc.get_history(rec.id)
    assert got.search(2).confirmed.entry_id == expected[2]
    assert got.search(0).confirmed.entry_id == expected[0]
    assert got.search(1).confirmed is None and got.search(3).confirmed is None
    assert np.allclose(svc.index.vector(ref2.photo_id), rec.searches[2].embedding)
    # a face confirmed for one entry cannot silently be confirmed for another
    from facedict.service import ServiceError
    with pytest.raises(ServiceError):
        svc.confirm(rec.id, expected[1], face_index=2)
    # undo face 2 only
    item = svc.unconfirm(rec.id, face_index=2)
    got = svc.get_history(rec.id)
    assert got.search(2).confirmed is None and got.search(0).confirmed is not None
    assert svc.get_entry(expected[2]).photo(ref2.photo_id) is None
    svc.restore(item.id)
    assert svc.get_history(rec.id).search(2).confirmed.added_photo_id == ref2.photo_id
    # removing face 0's album photo clears face 0's confirmation only
    svc.remove_photo(expected[0], ref0.photo_id)
    got = svc.get_history(rec.id)
    assert got.search(0).confirmed is None and got.search(2).confirmed is not None


def test_selected_face_is_remembered_and_kept_on_rerun(svc, group):
    data, expected = group
    rec = svc.query(data).history
    svc.set_selected(rec.id, 3)
    assert svc.get_history(rec.id).selected == 3
    assert svc.get_history(rec.id).matches[0].entry_id == expected[3]
    from facedict.service import ServiceError
    with pytest.raises(ServiceError):
        svc.set_selected(rec.id, 9)
    again = svc.rerun(rec.id)
    assert again.selected == 3 and svc.get_history(again.history.id).selected == 3


def test_cli_query_face_option(svc, group, isolated_vault, tmp_path):
    import subprocess
    import sys
    from tests.conftest import ROOT
    data, expected = group
    photo = tmp_path / "group.jpg"
    photo.write_bytes(data)
    name3 = svc.get_entry(expected[3]).name
    svc.lock()
    out = subprocess.run([sys.executable, "-m", "facedict", "--vault", str(isolated_vault), "query", str(photo),
                          "--face", "4", "--no-history", "--password-stdin"],
                         input=PW + "\n", capture_output=True, text=True, cwd=ROOT, timeout=120)
    assert out.returncode == 0, out.stderr
    assert "4 face(s) found" in out.stdout and out.stdout.splitlines()[1].strip().startswith(f"1. {name3}")
    bad = subprocess.run([sys.executable, "-m", "facedict", "--vault", str(isolated_vault), "query", str(photo),
                          "--face", "7", "--no-history", "--password-stdin"],
                         input=PW + "\n", capture_output=True, text=True, cwd=ROOT, timeout=120)
    assert bad.returncode == 1 and "between 1 and 4" in bad.stdout


# --- fields, search and ordering --------------------------------------------------------------------

def test_global_and_own_fields(svc):
    a, b = svc.list_entries()[0], svc.list_entries()[1]
    met = svc.add_global_field("Met at")
    with pytest.raises(ServiceError):
        svc.add_global_field(" met AT ")                       # names are unique, case-insensitively
    assert "Met at" in [f.name for f in svc.global_fields()]
    a.fields[met.id] = "the 2019 conference"
    a.own.append(OwnField("Phone", "555-11"))
    svc.update_entry(a)
    stored = svc.get_entry(a.id)
    assert stored.fields[met.id] == "the 2019 conference"
    assert ("Phone", "555-11") in [(f.name, f.value) for f in stored.own]
    assert svc.get_entry(b.id).fields.get(met.id, "") == ""    # shared field, but each entry has its own text
    # removing a shared field keeps whatever was written in it, as a field of that entry
    kept = svc.remove_global_field(met.id)
    assert kept == 1 and "Met at" not in [f.name for f in svc.global_fields()]
    after = svc.get_entry(a.id)
    assert ("Met at", "the 2019 conference") in [(f.name, f.value) for f in after.own]
    assert met.id not in after.fields
    assert "Met at" not in [f.name for f in svc.get_entry(b.id).own]


def test_a_removed_shared_field_can_be_put_back(svc):
    """What undo needs: the same field id, in the same place, with every entry's text back in it."""
    a, b = svc.list_entries()[0], svc.list_entries()[1]
    existing = [f.id for f in svc.global_fields()]
    city = svc.add_global_field("City")
    hobby = svc.add_global_field("Hobby")
    a.fields[city.id] = "Lisbon"
    svc.update_entry(a)
    b.fields[city.id] = ""
    b.own.append(OwnField("City", "already mine"))
    svc.update_entry(b)
    removed = svc.drop_global_field(city.id)
    assert removed.position == len(existing) and removed.converted == [a.id]
    assert removed.values == {a.id: "Lisbon", b.id: ""}
    assert [f.id for f in svc.global_fields()] == existing + [hobby.id]
    assert svc.get_entry(a.id).own_field("City").value == "Lisbon"
    svc.reinstate_global_field(removed)
    assert [f.id for f in svc.global_fields()] == existing + [city.id, hobby.id]        # same id, same place
    back = svc.get_entry(a.id)
    assert back.fields[city.id] == "Lisbon" and back.own_field("City") is None
    assert svc.get_entry(b.id).own_field("City").value == "already mine"       # not made by the removal
    with pytest.raises(ServiceError):
        svc.reinstate_global_field(removed)                                  # it is there already
    assert svc.drop_global_field("0" * 32) is None


def test_search_entries_by_scope(svc):
    field = svc.add_global_field("City")
    a, b = svc.list_entries()[0], svc.list_entries()[1]
    a.fields[field.id] = "Lisbon"
    a.aliases = ["Sunny"]
    a.tags = ["friend"]
    svc.update_entry(a)
    b.own.append(OwnField("City", "Lisbon of the north"))       # a field of that entry only
    svc.update_entry(b)
    assert [e.id for e in svc.search_entries("lisbon")] == [a.id]           # own fields are not searched
    assert [e.id for e in svc.search_entries("sunny")] == [a.id]
    assert [e.id for e in svc.search_entries("friend", scope="tags")] == [a.id]
    assert svc.search_entries("lisbon", scope="name") == []
    assert [e.id for e in svc.search_entries("lis", scope=field.id)] == [a.id]
    assert len(svc.search_entries("")) == len(svc.list_entries())


def test_photo_notes_survive_export_and_import(svc, tmp_path):
    entry = svc.list_entries()[0]
    first = entry.photos[0].photo_id
    assert svc.set_photo_note(entry.id, first, "  at the lake  ").note == "at the lake"
    out = svc.export_entry(entry.id, tmp_path / "export")
    doc = json.loads((out / "entry.json").read_text("utf-8"))
    assert list(doc["photo_notes"].values()) == ["at the lake"]
    svc.delete_entry(entry.id)
    svc.import_folder(out.parent)
    back = svc.find_entry_by_name(entry.name)
    assert [p.note for p in back.photos if p.note] == ["at the lake"]
    assert "photo_notes" not in [f.name for f in back.own]                 # not turned into a field


def test_photo_order_sets_the_cover(svc):
    entry = svc.list_entries()[0]
    ids = [p.photo_id for p in entry.photos]
    assert entry.cover.photo_id == ids[0]
    reordered = svc.set_photo_order(entry.id, list(reversed(ids)))
    assert [p.photo_id for p in reordered.photos] == list(reversed(ids))
    assert svc.get_entry(entry.id).cover.photo_id == ids[-1]
    with pytest.raises(ServiceError):
        svc.set_photo_order(entry.id, ids[:-1])


def test_databases_of_the_first_version_are_migrated(isolated_vault, models_available):
    """The old `notes` becomes the shared Notes field; imported extra keys become own fields."""
    from facedict.vault import Vault
    vault, _ = Vault.create(isolated_vault, PW, kdf_n=2 ** 12)
    old = {"id": new_id(), "name": "Old Entry", "aliases": [], "tags": ["x"], "notes": "remember this",
           "extra": {"birthday": "1970-01-01"}, "photos": [], "created_at": now_iso(), "updated_at": now_iso()}
    vault._put("entry", old["id"], json.dumps(old).encode())
    vault.lock()
    svc = FaceDictionaryService(isolated_vault)
    svc.unlock(PW)
    entry = svc.get_entry(old["id"])
    notes = next(f for f in svc.global_fields() if f.name == "Notes")
    assert entry.fields[notes.id] == "remember this"
    assert [(f.name, f.value) for f in entry.own] == [("birthday", "1970-01-01")]
    assert not entry.needs_migration
    svc.lock()
    svc.unlock(PW)                                              # nothing left to migrate
    assert svc.migrate_entries() == 0
