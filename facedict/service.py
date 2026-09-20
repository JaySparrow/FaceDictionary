"""Application layer shared by the GUI and the command line.

Owns the unlocked vault, the face engine and the in-memory face index, and keeps them consistent.
"""

from __future__ import annotations

import fcntl
import hashlib
import io
import json
import os
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import numpy as np
from PIL import Image

from . import config, databases
from .engine import FaceEngine, reading_order, thumbnail
from .imageio import ImageDecodeError, decode, encode_jpeg, is_supported
from .index import FaceIndex
from .models import (Confirmation, Entry, Face, FaceSearch, FieldDef, HistoryRecord, Match, OwnField, PhotoRef,
                     TrashItem, fold, new_id, now_iso)
from .vault import Vault, VaultError, VaultInfo, VaultLockedError

Progress = Callable[[int, int], None]
Cancel = Callable[[], bool]

DEFAULT_SETTINGS = {"recognizer": config.DEFAULT_RECOGNIZER, "auto_lock_minutes": config.AUTO_LOCK_SECONDS // 60,
                    "clear_clipboard_after_search": False}


class ServiceError(Exception):
    pass


class VaultInUseError(ServiceError):
    pass


@dataclass
class QueryResult:
    image: np.ndarray                  # decoded query image (BGR), memory only
    searches: list[FaceSearch]         # every face found, in reading order, each with its own top-k
    selected: int                      # the face shown first (the largest)
    history: HistoryRecord | None
    elapsed_ms: float
    low_threshold: float = 0.3

    @property
    def faces(self) -> list[Face]:
        return [s.face for s in self.searches]

    @property
    def face(self) -> Face | None:
        return self.searches[self.selected].face if self.searches else None

    @property
    def matches(self) -> list[Match]:
        return self.searches[self.selected].matches if self.searches else []

    def low_confidence_for(self, index: int) -> bool:
        m = self.searches[index].matches
        return not m or m[0].score < self.low_threshold

    @property
    def low_confidence(self) -> bool:
        return bool(self.searches) and self.low_confidence_for(self.selected)


@dataclass
class AddPhotoResult:
    photo: PhotoRef | None
    face_count: int = 0
    duplicate: bool = False
    error: str | None = None


@dataclass
class ImportReport:
    entries_created: int = 0
    entries_updated: int = 0
    photos_added: int = 0
    duplicates: int = 0
    no_face: int = 0
    multi_face: int = 0
    errors: list[str] = field(default_factory=list)


SCOPE_ALL, SCOPE_NAME, SCOPE_TAGS = "all", "name", "tags"


def haystacks(entry: Entry, field_ids: list[str]) -> dict[str, str]:
    """Lower-cased text of an entry per search scope: everything, the name(s), the tags, each global field."""
    per_field = {fid: entry.fields.get(fid, "") for fid in field_ids}
    names = " ".join([entry.name, *entry.aliases])
    tags = " ".join(entry.tags)
    everything = " ".join([names, tags, *per_field.values()])
    out = {SCOPE_ALL: fold(everything), SCOPE_NAME: fold(names), SCOPE_TAGS: fold(tags)}
    out.update({fid: fold(text) for fid, text in per_field.items()})
    return out


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _safe_filename(name: str) -> str:
    cleaned = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', "_", name).strip(" .")
    return cleaned[:100] or "entry"


class FaceDictionaryService:
    def __init__(self, vault_root: Path | None = None, models_path: Path | None = None):
        self.vault_root = Path(vault_root).expanduser() if vault_root else databases.startup_vault()
        self.models_path = models_path
        self.vault: Vault | None = None
        self.index: FaceIndex | None = None
        self.settings = dict(DEFAULT_SETTINGS)
        self._engines: dict[str, FaceEngine] = {}
        self._index_dirty = False
        self._lock = threading.RLock()
        self._inuse_fd: int | None = None

    # --- vault lifecycle -------------------------------------------------------------------

    def vault_exists(self) -> bool:
        return Vault.exists(self.vault_root)

    @property
    def vault_name(self) -> str:
        return databases.display_name(self.vault_root)

    def inspect(self, path: Path | None = None) -> VaultInfo:
        """What kind of database is at this path (no password needed)?"""
        return Vault.inspect(Path(path).expanduser() if path else self.vault_root)

    def use_database(self, path: Path) -> VaultInfo:
        """Point the app at another database. Only possible while locked."""
        if self.unlocked:
            raise ServiceError("Lock the current database first")
        self.vault_root = Path(path).expanduser().resolve()
        self.index = None
        return self.inspect()

    def recent_databases(self) -> list[databases.DatabaseRef]:
        return databases.recent()

    @property
    def unlocked(self) -> bool:
        return self.vault is not None and not self.vault.locked

    def _v(self) -> Vault:
        if self.vault is None or self.vault.locked:
            raise VaultLockedError("The vault is locked")
        return self.vault

    def _claim(self) -> None:
        """Only one app instance may have the vault open, so they cannot overwrite each other's changes."""
        if self._inuse_fd is not None:
            return
        fd = os.open(self.vault_root / ".inuse", os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            raise VaultInUseError("The vault is already open in another FaceDictionary window.") from None
        self._inuse_fd = fd

    def _release(self) -> None:
        if self._inuse_fd is not None:
            fcntl.flock(self._inuse_fd, fcntl.LOCK_UN)
            os.close(self._inuse_fd)
            self._inuse_fd = None

    def _open_with(self, opener) -> None:
        with self._lock:
            if self.vault_exists():
                self._claim()
            try:
                self.vault = opener()
                self._claim()
                self._load_settings()
                databases.remember(self.vault_root)
            except BaseException:
                self.vault = None
                self._release()
                raise

    def create_vault(self, password: str, kdf_n: int = 2 ** 17, recovery_code: bool = False) -> str | None:
        codes = []

        def create():
            vault, code = Vault.create(self.vault_root, password, kdf_n=kdf_n, recovery_code=recovery_code)
            codes.append(code)
            return vault

        self._open_with(create)
        return codes[0]

    def unlock(self, password: str) -> None:
        self._open_with(lambda: Vault.open(self.vault_root, password))

    def recover(self, recovery_code: str, new_password: str) -> None:
        self._open_with(lambda: Vault.recover(self.vault_root, recovery_code, new_password))

    def recover_with_answers(self, answers: dict[int, str], new_password: str) -> None:
        self._open_with(lambda: Vault.recover_with_answers(self.vault_root, answers, new_password))

    def set_questions(self, password: str, questions: list[str], answers: list[str], required: int = 2) -> None:
        self._v().set_questions(password, questions, answers, required)

    def clear_recovery(self, password: str, kind: str) -> None:
        self._v().clear_recovery(password, kind)

    def change_password(self, old: str, new: str) -> None:
        self._v().change_password(old, new)

    def check_password(self, password: str) -> None:
        """Raises WrongPasswordError if the password is not this database's password."""
        self._v()._check_password(password)

    def new_recovery_code(self, password: str) -> str:
        return self._v().new_recovery_code(password)

    def lock(self) -> None:
        """Drop keys, index and caches from memory. Never touches the disk."""
        with self._lock:
            if self.vault is not None:
                self.vault.lock()
            self.vault = None
            self.index = None
            self._index_dirty = False
            self.settings = dict(DEFAULT_SETTINGS)
            self._release()

    # --- settings ----------------------------------------------------------------------------

    def _load_settings(self) -> None:
        stored = self._v().get_meta("settings") or {}
        self.settings = {**DEFAULT_SETTINGS, **stored}
        if self.settings["recognizer"] not in config.recognizer_names():
            self.settings["recognizer"] = config.DEFAULT_RECOGNIZER
        self.index = None
        self.migrate_entries()

    def set_setting(self, key: str, value) -> None:
        if key not in DEFAULT_SETTINGS:
            raise KeyError(key)
        self.settings[key] = value
        self._v().put_meta("settings", self.settings)

    # --- engine & index ------------------------------------------------------------------------

    @property
    def recognizer(self) -> str:
        return self.settings["recognizer"]

    def available_recognizers(self) -> list[str]:
        root = Path(self.models_path) if self.models_path else config.models_dir()
        return [n for n in config.recognizer_names() if (root / config.MODELS[n].filename).exists()]

    def engine(self, name: str | None = None) -> FaceEngine:
        name = name or self.recognizer
        with self._lock:
            if name not in self._engines:
                self._engines[name] = FaceEngine(name, models_path=self.models_path)
            return self._engines[name]

    def set_recognizer(self, name: str, progress: Progress | None = None, cancel: Cancel | None = None) -> dict:
        if name not in self.available_recognizers():
            raise ServiceError(f"Model not available: {name}")
        self.engine(name)                                  # fail early if the model is broken
        with self._lock:
            self.set_setting("recognizer", name)
            self.index = None
            return self.ensure_index(progress, cancel)

    def ensure_index(self, progress: Progress | None = None, cancel: Cancel | None = None) -> dict:
        """Bring the index in line with the entries: drop stale photos, embed missing ones."""
        with self._lock:
            v = self._v()
            eng = self.engine()
            if self.index is None or self.index.model != eng.model_name:
                idx = None
                data = v.load_index(eng.model_name)
                if data:
                    try:
                        idx = FaceIndex.from_bytes(data)
                    except Exception:
                        idx = None
                if idx is None or idx.dim != eng.dim or idx.model != eng.model_name:
                    idx = FaceIndex(eng.model_name, eng.dim)
                self.index = idx
            idx = self.index
            wanted: dict[str, tuple[str, PhotoRef]] = {}
            for entry in v.list_entries():
                for ref in entry.photos:
                    if ref.face is not None:
                        wanted[ref.photo_id] = (entry.id, ref)
            removed = idx.remove([p for p in idx.photo_ids if p not in wanted])
            for pid in list(idx.photo_ids):                 # photo moved to another entry
                if idx.entry_of(pid) != wanted[pid][0]:
                    idx.add(pid, wanted[pid][0], idx.vector(pid))
                    removed += 1
            missing = [pid for pid in wanted if pid not in idx]
            computed, errors = 0, 0
            for i, pid in enumerate(missing):
                if cancel and cancel():
                    break
                entry_id, ref = wanted[pid]
                try:
                    img = decode(v.get_photo(pid))
                    idx.add(pid, entry_id, eng.embed(img, ref.face))
                    computed += 1
                except (VaultError, ImageDecodeError, ValueError):
                    errors += 1
                if progress:
                    progress(i + 1, len(missing))
            if computed or removed:
                self._index_dirty = True
            self.flush_index()
            return {"total": len(idx), "computed": computed, "removed": removed, "errors": errors,
                    "complete": computed + errors == len(missing)}

    def flush_index(self) -> None:
        with self._lock:
            if self._index_dirty and self.index is not None and self.unlocked:
                self._v().save_index(self.index.model, self.index.to_bytes())
                self._index_dirty = False

    def _index_add(self, photo_id: str, entry_id: str, vector: np.ndarray, model: str, flush: bool = True) -> None:
        with self._lock:
            if model != self.recognizer:
                return
            if self.index is None or self.index.model != model:
                self.ensure_index()                        # full sync picks up the new photo as well
                return
            self.index.add(photo_id, entry_id, vector)
            self._index_dirty = True
            if flush:
                self.flush_index()

    def _index_remove(self, photo_ids: list[str]) -> None:
        with self._lock:
            if self.index is not None and self.index.remove(photo_ids):
                self._index_dirty = True
                self.flush_index()

    # --- search & history ------------------------------------------------------------------------

    def query(self, data: bytes, save_history: bool = True) -> QueryResult:
        """Search every face in the photo. Faces are kept in reading order; the largest is selected."""
        t0 = time.perf_counter()
        v = self._v()
        img = decode(data)
        eng = self.engine()
        if self.index is None or self.index.model != eng.model_name:
            self.ensure_index()                           # all later changes keep the index in sync
        detected = eng.detect(img)                        # largest first
        if not detected:
            return QueryResult(img, [], 0, None, (time.perf_counter() - t0) * 1000, eng.spec.low_confidence)
        order = reading_order(detected)
        faces = [detected[i] for i in order]
        selected = order.index(0)
        names: dict[str, str] = {}
        searches = []
        for face in faces:
            emb = eng.embed(img, face)
            with self._lock:
                hits = self.index.search(emb, k=config.TOP_K)
            matches = []
            for rank, hit in enumerate(hits, 1):
                if hit.entry_id not in names:
                    try:
                        names[hit.entry_id] = v.get_entry(hit.entry_id).name
                    except VaultError:
                        names[hit.entry_id] = "(deleted)"
                matches.append(Match(rank, hit.entry_id, names[hit.entry_id], hit.score, hit.photo_id))
            searches.append(FaceSearch(face=face, embedding=emb, matches=matches))
        record = None
        if save_history:
            pid = v.put_photo(data)
            record = v.save_history(HistoryRecord(id=new_id(), time=now_iso(), photo_id=pid, model=eng.model_name,
                                                  searches=searches, selected=selected))
            self._store_thumb(pid, img, faces[selected])
        return QueryResult(img, searches, selected, record, (time.perf_counter() - t0) * 1000,
                           eng.spec.low_confidence)

    def rerun(self, history_id: str) -> QueryResult:
        """Search the photo of a past record again with the current library (saved as a new record)."""
        v = self._v()
        old = v.get_history(history_id)
        res = self.query(v.get_photo(old.photo_id))
        if res.history is not None and old.face is not None and len(res.searches) == old.face_count:
            res.selected = old.selected                   # keep looking at the same face
            self.set_selected(res.history.id, old.selected)
            res.history = self.get_history(res.history.id)
        return res

    def list_history(self) -> list[HistoryRecord]:
        return self._v().list_history()

    def get_history(self, history_id: str) -> HistoryRecord:
        return self._v().get_history(history_id)

    def set_selected(self, history_id: str, face_index: int) -> HistoryRecord:
        """Remember which face of a search the user looked at last."""
        with self._lock:
            v = self._v()
            rec = v.get_history(history_id)
            if not 0 <= face_index < rec.face_count:
                raise ServiceError("No such face in this search")
            if rec.selected != face_index:
                rec.selected = face_index
                v.save_history(rec)
            return rec

    def confirm(self, history_id: str, entry_id: str, face_index: int | None = None) -> PhotoRef:
        """The user confirmed that a face in the searched photo is this entry's person:
        add the photo to the entry's album, using that face and its stored embedding."""
        with self._lock:
            v = self._v()
            rec = v.get_history(history_id)
            i = rec.selected if face_index is None else face_index
            search = rec.search(i)
            if search is None:
                raise ServiceError("No such face in this search")
            if search.confirmed is not None and search.confirmed.entry_id != entry_id:
                raise ServiceError("This face is already confirmed as another entry. Undo that first.")
            entry = v.get_entry(entry_id)
            data = v.get_photo(rec.photo_id)
            digest = sha256(data)
            ref = next((p for p in entry.photos if p.sha256 == digest), None)
            if ref is None:
                pid = v.put_photo(data)
                ref = PhotoRef(photo_id=pid, sha256=digest, face=search.face, face_count=rec.face_count,
                               source=f"query:{rec.id}#{i}")
                entry.photos.append(ref)
                v.save_entry(entry)
                self._store_thumb(pid, decode(data), search.face)
                if search.embedding is not None and self.index is not None and rec.model == self.index.model:
                    self._index_add(pid, entry_id, search.embedding, rec.model)       # no recomputation
                else:
                    self.ensure_index()
            search.confirmed = Confirmation(entry_id=entry_id, added_photo_id=ref.photo_id)
            v.save_history(rec)
            return ref

    def unconfirm(self, history_id: str, face_index: int | None = None) -> TrashItem | None:
        """Undo a confirmation; the photo added from this search goes to the trash."""
        with self._lock:
            v = self._v()
            rec = v.get_history(history_id)
            i = rec.selected if face_index is None else face_index
            search = rec.search(i)
            if search is None or search.confirmed is None:
                return None
            conf = search.confirmed
            item = None
            try:
                entry = v.get_entry(conf.entry_id)
                ref = entry.photo(conf.added_photo_id)
                if ref is not None and ref.from_query == rec.id and ref.query_face == i:
                    item = self.remove_photo(entry.id, ref.photo_id)
            except VaultError:
                pass
            self._set_confirmation(history_id, i, None)
            return item

    def delete_history(self, history_id: str) -> TrashItem:
        return self._v().delete_history(history_id)

    # --- entries -----------------------------------------------------------------------------------

    def list_entries(self) -> list[Entry]:
        return self._v().list_entries()

    def get_entry(self, entry_id: str) -> Entry:
        return self._v().get_entry(entry_id)

    def find_entry_by_name(self, name: str) -> Entry | None:
        key = fold(name)
        return next((e for e in self.list_entries() if fold(e.name) == key), None)

    def create_entry(self, name: str, aliases=(), tags=(), fields: dict | None = None,
                     own: list[OwnField] | None = None) -> Entry:
        name = name.strip()
        if not name:
            raise ServiceError("An entry needs a name")
        return self._v().new_entry(name, aliases=list(aliases), tags=list(tags), fields=dict(fields or {}),
                                   own=list(own or []))

    def update_entry(self, entry: Entry) -> Entry:
        """Save the edited text of an entry. The photo list is taken from the stored entry."""
        v = self._v()
        stored = v.get_entry(entry.id)
        if not entry.name.strip():
            raise ServiceError("An entry needs a name")
        known = {f.id for f in self.global_fields()}
        stored.name, stored.aliases, stored.tags = entry.name.strip(), list(entry.aliases), list(entry.tags)
        stored.fields = {k: v_ for k, v_ in entry.fields.items() if k in known}
        stored.own = [OwnField(f.name.strip(), f.value) for f in entry.own if f.name.strip()]
        return v.save_entry(stored)

    # --- fields ---------------------------------------------------------------------------------------

    def global_fields(self) -> list[FieldDef]:
        return self._v().get_field_defs()

    def add_global_field(self, name: str) -> FieldDef:
        """Add a field that every entry of this database gets."""
        name = name.strip()
        if not name:
            raise ServiceError("A field needs a name")
        with self._lock:
            defs = self.global_fields()
            if any(fold(f.name) == fold(name) for f in defs):
                raise ServiceError(f"There is already a field called “{name}”")
            field_def = FieldDef(id=new_id(), name=name)
            self._v().set_field_defs(defs + [field_def])
            return field_def

    def rename_global_field(self, field_id: str, name: str) -> None:
        name = name.strip()
        if not name:
            raise ServiceError("A field needs a name")
        with self._lock:
            defs = self.global_fields()
            if any(f.id != field_id and fold(f.name) == fold(name) for f in defs):
                raise ServiceError(f"There is already a field called “{name}”")
            for f in defs:
                if f.id == field_id:
                    f.name = name
            self._v().set_field_defs(defs)

    def remove_global_field(self, field_id: str, keep_content: bool = True) -> int:
        """Remove a global field from the database. Entries that had text in it keep that text as a
        field of their own (unless `keep_content` is off). Returns how many entries kept content."""
        with self._lock:
            v = self._v()
            defs = self.global_fields()
            gone = next((f for f in defs if f.id == field_id), None)
            if gone is None:
                return 0
            kept = 0
            for entry in v.list_entries():
                if field_id not in entry.fields:
                    continue
                text = entry.fields.pop(field_id)
                if text and keep_content and entry.own_field(gone.name) is None:
                    entry.own.append(OwnField(gone.name, text))
                    kept += 1
                v.save_entry(entry)
            v.set_field_defs([f for f in defs if f.id != field_id])
            return kept

    # --- photos order ----------------------------------------------------------------------------------

    def set_photo_order(self, entry_id: str, photo_ids: list[str]) -> Entry:
        """Reorder an entry's album; the first photo is the entry's cover."""
        v = self._v()
        entry = v.get_entry(entry_id)
        by_id = {p.photo_id: p for p in entry.photos}
        if set(photo_ids) != set(by_id):
            raise ServiceError("The new order must list exactly the entry's photos")
        entry.photos = [by_id[pid] for pid in photo_ids]
        return v.save_entry(entry)

    # --- searching entries ------------------------------------------------------------------------------

    def search_entries(self, query: str, scope: str = SCOPE_ALL) -> list[Entry]:
        """Entries whose text contains `query`. Scope is "all" (names, tags and every global field),
        "name", "tags", or the id of a global field."""
        needle = fold(query)
        entries = self.list_entries()
        if not needle:
            return entries
        field_ids = [f.id for f in self.global_fields()]
        return [e for e in entries if needle in haystacks(e, field_ids).get(scope, "")]

    # --- migration of databases made by the first version -------------------------------------------------

    def migrate_entries(self) -> int:
        """Move the old `notes` into a global “Notes” field and imported extra keys into own fields."""
        with self._lock:
            v = self._v()
            pending = [e for e in v.list_entries() if e.needs_migration]
            if not pending:
                return 0
            defs = v.get_field_defs()
            notes = next((f for f in defs if fold(f.name) == "notes"), None)
            if notes is None and any(e.legacy_notes for e in pending):
                notes = FieldDef(id=new_id(), name="Notes")
                defs = defs + [notes]
                v.set_field_defs(defs)
            for entry in pending:
                if entry.legacy_notes and notes is not None:
                    entry.fields[notes.id] = entry.legacy_notes
                for key, value in entry.legacy_extra.items():
                    if entry.own_field(str(key)) is None:
                        entry.own.append(OwnField(str(key), str(value)))
                entry.legacy_notes, entry.legacy_extra = "", {}
                v.save_entry(entry)
            return len(pending)

    def add_photo(self, entry_id: str, data: bytes, flush: bool = True) -> AddPhotoResult:
        v = self._v()
        entry = v.get_entry(entry_id)
        try:
            img = decode(data)
        except ImageDecodeError as exc:
            return AddPhotoResult(None, error=str(exc))
        digest = sha256(data)
        if any(p.sha256 == digest for p in entry.photos):
            return AddPhotoResult(None, duplicate=True)
        eng = self.engine()
        faces = eng.detect(img)
        face = faces[0] if faces else None
        pid = v.put_photo(data)
        ref = PhotoRef(photo_id=pid, sha256=digest, face=face, face_count=len(faces), source="import")
        entry.photos.append(ref)
        v.save_entry(entry)
        self._store_thumb(pid, img, face)
        if face is not None:
            self._index_add(pid, entry_id, eng.embed(img, face), eng.model_name, flush=flush)
        return AddPhotoResult(ref, face_count=len(faces))

    def detect_faces(self, photo_id: str) -> tuple[np.ndarray, list[Face]]:
        img = decode(self._v().get_photo(photo_id))
        return img, self.engine().detect(img)

    def choose_face(self, entry_id: str, photo_id: str, face: Face | None) -> PhotoRef:
        """Pick which detected face in an album photo belongs to this entry (None = don't use for matching)."""
        v = self._v()
        entry = v.get_entry(entry_id)
        ref = entry.photo(photo_id)
        if ref is None:
            raise ServiceError("Photo not found in entry")
        img = decode(v.get_photo(photo_id))
        ref.face = face
        v.save_entry(entry)
        self._store_thumb(photo_id, img, face)
        if face is None:
            self._index_remove([photo_id])
        else:
            eng = self.engine()
            self._index_add(photo_id, entry_id, eng.embed(img, face), eng.model_name)
        return ref

    def remove_photo(self, entry_id: str, photo_id: str) -> TrashItem:
        item = self._v().remove_photo(entry_id, photo_id)
        self._index_remove([photo_id])
        ref = PhotoRef.from_dict(item.payload["photo_ref"])
        if ref.from_query:
            self._set_confirmation(ref.from_query, ref.query_face, None, only_photo=photo_id)
        return item

    def delete_entry(self, entry_id: str) -> TrashItem:
        item = self._v().delete_entry(entry_id)
        with self._lock:
            if self.index is not None and self.index.remove_entry(entry_id):
                self._index_dirty = True
                self.flush_index()
        return item

    def _set_confirmation(self, history_id: str, face_index: int, confirmation: Confirmation | None,
                          only_photo: str | None = None) -> None:
        """Set or clear one face's confirmation. With `only_photo`, clear it only if it points to that photo."""
        with self._lock:
            v = self._v()
            try:
                rec = v.get_history(history_id)
            except VaultError:
                return
            search = rec.search(face_index)
            if search is None:
                return
            if only_photo is not None and (search.confirmed is None or search.confirmed.added_photo_id != only_photo):
                return
            if confirmation is None and search.confirmed is None:
                return
            search.confirmed = confirmation
            v.save_history(rec)

    # --- trash -----------------------------------------------------------------------------------------

    def list_trash(self) -> list[TrashItem]:
        return self._v().list_trash()

    def restore(self, trash_id: str) -> TrashItem:
        item = self._v().restore(trash_id)
        if item.kind in ("photo", "entry"):
            self.ensure_index()
        if item.kind == "photo":
            ref = PhotoRef.from_dict(item.payload["photo_ref"])
            if ref.from_query:
                self._set_confirmation(ref.from_query, ref.query_face,
                                       Confirmation(item.payload["entry_id"], ref.photo_id))
        return item

    def purge(self, trash_id: str) -> None:
        self._v().purge(trash_id)

    # --- images for display -------------------------------------------------------------------------------

    def photo_bytes(self, photo_id: str) -> bytes:
        return self._v().get_photo(photo_id)

    def photo_image(self, photo_id: str) -> np.ndarray:
        return decode(self.photo_bytes(photo_id))

    def thumb_bytes(self, photo_id: str, face: Face | None = None) -> bytes:
        v = self._v()
        thumb = v.get_thumb(photo_id)
        if thumb is None:
            img = decode(v.get_photo(photo_id))
            thumb = self._store_thumb(photo_id, img, face)
        return thumb

    def _store_thumb(self, photo_id: str, img: np.ndarray, face: Face | None) -> bytes:
        thumb = encode_jpeg(thumbnail(img, face), quality=85)
        self._v().put_thumb(photo_id, thumb)
        return thumb

    # --- import / export ------------------------------------------------------------------------------------

    def import_folder(self, root: Path, progress: Progress | None = None, cancel: Cancel | None = None) -> ImportReport:
        """Import people from `root/<person>/` folders (each may hold an entry.json and photos).
        A folder that directly contains photos is imported as a single person.
        Source files are only read, never modified, moved or deleted."""
        root = Path(root)
        report = ImportReport()
        folders = sorted(d for d in root.iterdir() if d.is_dir() and not d.name.startswith("."))
        if any(is_supported(f) for f in root.iterdir() if f.is_file()):
            folders = [root]
        jobs = [(d, sorted(f for f in d.iterdir() if f.is_file() and is_supported(f) and not f.name.startswith(".")))
                for d in folders]
        total = sum(len(files) for _, files in jobs)
        done = 0
        try:
            for folder, files in jobs:
                if cancel and cancel():
                    break
                meta = {}
                if (folder / "entry.json").is_file():
                    try:
                        meta = json.loads((folder / "entry.json").read_text("utf-8"))
                    except (OSError, ValueError) as exc:
                        report.errors.append(f"{folder.name}/entry.json: {exc}")
                name = str(meta.get("name") or folder.name.replace("_", " ")).strip()
                entry = self.find_entry_by_name(name)
                if entry is None:
                    if not files and not meta:
                        continue
                    fields, own = self._fields_from_meta(meta)
                    entry = self.create_entry(name, aliases=meta.get("aliases", []), tags=meta.get("tags", []),
                                              fields=fields, own=own)
                    report.entries_created += 1
                else:
                    report.entries_updated += 1
                for f in files:
                    if cancel and cancel():
                        break
                    try:
                        res = self.add_photo(entry.id, f.read_bytes(), flush=False)
                    except OSError as exc:
                        res = AddPhotoResult(None, error=str(exc))
                    if res.error:
                        report.errors.append(f"{folder.name}/{f.name}: {res.error}")
                    elif res.duplicate:
                        report.duplicates += 1
                    else:
                        report.photos_added += 1
                        report.no_face += res.face_count == 0
                        report.multi_face += res.face_count > 1
                    done += 1
                    if progress:
                        progress(done, total)
        finally:
            self.flush_index()
        return report

    def _fields_from_meta(self, meta: dict) -> tuple[dict, list[OwnField]]:
        """entry.json keys become values of the global field with that name, or fields of this entry.
        “notes” always becomes the global Notes field."""
        skip = {"id", "name", "aliases", "tags", "photos", "created_at", "updated_at", "fields", "own"}
        pairs: list[tuple[str, str]] = []
        for key, value in meta.items():
            if key in skip:
                continue
            if key == "extra" and isinstance(value, dict):
                pairs += [(str(k), str(v)) for k, v in value.items()]
            else:
                pairs.append((str(key), value if isinstance(value, str) else str(value)))
        by_name = {fold(f.name): f for f in self.global_fields()}
        fields, own = {}, []
        for key, text in pairs:
            spec = by_name.get(fold(key))
            if spec is None and fold(key) == "notes":
                spec = self.add_global_field("Notes")
                by_name[fold(spec.name)] = spec
            if spec is not None:
                fields[spec.id] = text
            else:
                own.append(OwnField(key, text))
        return fields, own

    def export_entry(self, entry_id: str, dest_dir: Path) -> Path:
        """Write a PLAINTEXT copy of one entry (entry.json + original photos) into a new folder."""
        v = self._v()
        entry = v.get_entry(entry_id)
        base = Path(dest_dir) / _safe_filename(entry.name)
        folder, n = base, 2
        while folder.exists():
            folder = base.with_name(f"{base.name} ({n})")
            n += 1
        folder.mkdir(parents=True)
        doc = {"name": entry.name, "aliases": entry.aliases, "tags": entry.tags}
        for spec in self.global_fields():
            if entry.fields.get(spec.id):
                doc[spec.name] = entry.fields[spec.id]
        for own in entry.own:
            doc.setdefault(own.name, own.value)
        doc["created_at"], doc["updated_at"] = entry.created_at, entry.updated_at
        (folder / "entry.json").write_text(json.dumps(doc, ensure_ascii=False, indent=2), "utf-8")
        for i, ref in enumerate(entry.photos, 1):
            data = v.get_photo(ref.photo_id)
            ext = ".jpg"
            try:
                with Image.open(io.BytesIO(data)) as im:
                    ext = {"JPEG": ".jpg", "PNG": ".png", "HEIF": ".heic", "WEBP": ".webp", "BMP": ".bmp",
                           "TIFF": ".tif"}.get(im.format or "", ".jpg")
            except Exception:
                pass
            (folder / f"photo_{i:03d}{ext}").write_bytes(data)
        return folder

    # --- misc ------------------------------------------------------------------------------------------------

    def stats(self) -> dict:
        v = self._v()
        entries = v.list_entries()
        return {"entries": len(entries), "photos": sum(len(e.photos) for e in entries),
                "indexed": len(self.index) if self.index is not None else 0,
                "history": len(v.list_history()), "trash": len(v.list_trash()), "model": self.recognizer}
