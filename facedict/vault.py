"""Encrypted on-disk store for entries, photos, thumbnails, search history and the face index.

Layout (every *.enc file is AES-256-GCM encrypted; names are random ids):

    vault.json              KDF parameters + the data key wrapped by the password and by the recovery code
    entries/<id>.enc        Entry (JSON)
    photos/<id>.enc         original photo bytes (JPEG/PNG/HEIC as imported)
    thumbs/<id>.enc         face thumbnail (JPEG) - rebuildable cache
    history/<id>.enc        HistoryRecord (JSON)
    index/<model>.enc       face embeddings (npz) - rebuildable cache
    meta/<name>.enc         small JSON documents such as settings
    trash/<id>/             deleted files, moved here unchanged, plus manifest.enc

Safety rules:
  * locking only drops keys and caches from memory; it never touches the disk;
  * deleting moves files into trash/; only `purge()` removes data permanently,
    and it only ever operates inside trash/;
  * all writes are atomic (temp file + fsync + rename).
"""

from __future__ import annotations

import itertools
import json
import os
import re
import secrets
import threading
from dataclasses import dataclass, field
from pathlib import Path

from . import config
from .crypto import (ScryptParams, TamperedError, WrongPasswordError, atomic_write, b64d, b64e,
                     derive_answers_key, derive_password_key, derive_recovery_key, new_recovery_code,
                     normalize_answer, random_key, seal, unseal)
from .models import Entry, FieldDef, HistoryRecord, PhotoRef, TrashItem, fold, new_id, now_iso

FORMAT = 2                       # 1: single recovery code; 2: recovery slots (questions and/or code)
SUPPORTED_FORMATS = (1, 2)
MAX_QUESTIONS = 6
MAX_COMBOS = 10
KEYFILE = "vault.json"
TRASH = "trash"
MANIFEST = "manifest.enc"
KIND_DIRS = {"entry": "entries", "photo": "photos", "thumb": "thumbs", "history": "history", "index": "index",
             "meta": "meta"}
_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_INDEX_ID_RE = re.compile(r"^[a-z0-9_]{1,64}$")


class VaultError(Exception):
    pass


class VaultLockedError(VaultError):
    pass


class VaultNotFoundError(VaultError):
    pass


class VaultExistsError(VaultError):
    pass


class RestoreConflictError(VaultError):
    pass


class UnsupportedVaultError(VaultError):
    pass


@dataclass
class VaultInfo:
    """What can be told about a folder without knowing its password."""
    path: Path
    is_vault: bool = False
    can_create: bool = False           # empty or missing folder: a new database can be made here
    format: int | None = None
    supported: bool = False
    questions: list[str] = field(default_factory=list)
    required: int = 0
    has_code: bool = False
    error: str | None = None

    @property
    def usable(self) -> bool:
        return self.is_vault and self.supported and self.error is None

    @property
    def recovery_methods(self) -> list[str]:
        methods = ["questions"] if self.questions else []
        return methods + (["code"] if self.has_code else [])


def _refuse_real_vault_in_tests(root: Path) -> None:
    if "PYTEST_CURRENT_TEST" in os.environ and root.resolve() == config.DEFAULT_VAULT_DIR:
        raise VaultError("Refusing to touch the real vault from a test run")


def _dumps(obj) -> bytes:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _recovery_slots(keyfile: dict) -> list[dict]:
    """Recovery slots of a keyfile, converting the single code of format 1."""
    rec = keyfile.get("recovery") or {}
    if isinstance(rec.get("slots"), list):
        return [s for s in rec["slots"] if isinstance(s, dict)]
    if rec.get("wrapped_key"):
        return [{"type": "code", "salt": rec["salt"], "wrapped_key": rec["wrapped_key"]}]
    return []


def _slot(keyfile: dict, kind: str) -> dict | None:
    return next((s for s in _recovery_slots(keyfile) if s.get("type") == kind), None)


def _with_slot(keyfile: dict, kind: str, slot: dict | None) -> dict:
    """A copy of the keyfile in the current format with `kind`'s slot replaced (or removed)."""
    slots = [s for s in _recovery_slots(keyfile) if s.get("type") != kind]
    if slot is not None:
        slots.append(slot)
    out = dict(keyfile)
    out["format"] = FORMAT
    out["recovery"] = {"slots": slots}
    return out


def _questions_slot(key: bytes, questions: list[str], answers: list[str], required: int, kdf_n: int) -> dict:
    """Wrap the data key once per allowed combination of answers, so any `required` of them open it."""
    if not 2 <= len(questions) <= MAX_QUESTIONS or len(answers) != len(questions):
        raise ValueError(f"Use between 2 and {MAX_QUESTIONS} questions, each with an answer")
    if not 1 <= required <= len(questions):
        raise ValueError("Invalid number of required answers")
    if len({q.strip().casefold() for q in questions}) != len(questions) or any(not q.strip() for q in questions):
        raise ValueError("The questions must be different and none may be empty")
    if any(not normalize_answer(a) for a in answers):
        raise ValueError("Every answer must contain at least one letter or digit")
    combos = list(itertools.combinations(range(len(questions)), required))
    if len(combos) > MAX_COMBOS:
        raise ValueError("Too many question combinations; use fewer questions")
    params = ScryptParams.generate(kdf_n)
    wrapped = []
    for idx in combos:
        combo_key = derive_answers_key([answers[i] for i in idx], params, idx)
        wrapped.append({"indexes": list(idx), "wrapped_key": b64e(seal(combo_key, key, b"key:questions"))})
    return {"type": "questions", "kdf": params.to_dict(), "questions": [q.strip() for q in questions],
            "required": required, "combos": wrapped}


class Vault:
    def __init__(self, root: Path, key: bytes, keyfile: dict):
        self.root = Path(root)
        self._key: bytes | None = key
        self._keyfile = keyfile
        self._lock = threading.RLock()
        self._entries: dict[str, Entry] | None = None
        self._history: dict[str, HistoryRecord] | None = None
        self.unreadable: set[str] = set()          # files that failed to decrypt / parse (see verify())

    # --- lifecycle --------------------------------------------------------------

    @staticmethod
    def exists(root: Path) -> bool:
        return (Path(root) / KEYFILE).is_file()

    @classmethod
    def inspect(cls, root: Path) -> VaultInfo:
        """Look at a folder without a password: is it a database, is its format supported,
        and how can its password be recovered?"""
        root = Path(root).expanduser()
        info = VaultInfo(path=root)
        keyfile_path = root / KEYFILE
        if not keyfile_path.is_file():
            if not root.exists():
                info.can_create = True
            elif not root.is_dir():
                info.error = "This is a file, not a folder."
            elif any(p.name != ".DS_Store" for p in root.iterdir()):
                info.error = "This folder is not a FaceDictionary database."
            else:
                info.can_create = True
            return info
        info.is_vault = True
        try:
            keyfile = json.loads(keyfile_path.read_text("utf-8"))
        except (OSError, ValueError):
            info.error = "The database's key file is damaged (vault.json)."
            return info
        info.format = keyfile.get("format") if isinstance(keyfile.get("format"), int) else None
        if keyfile.get("app") != "FaceDictionary" or info.format is None or not keyfile.get("wrapped_key"):
            info.error = "This folder is not a FaceDictionary database."
            return info
        if info.format not in SUPPORTED_FORMATS:
            info.error = (f"This database was made by a newer version of FaceDictionary "
                          f"(format {info.format}). Please update the app.")
            return info
        info.supported = True
        questions = _slot(keyfile, "questions")
        if questions:
            info.questions = [str(q) for q in questions.get("questions", [])]
            info.required = int(questions.get("required", len(info.questions)))
        info.has_code = _slot(keyfile, "code") is not None
        return info

    @classmethod
    def create(cls, root: Path, password: str, kdf_n: int = 2 ** 17,
               recovery_code: bool = False) -> tuple["Vault", str | None]:
        """Create a new vault. With `recovery_code`, also returns a one-time recovery code
        (security questions are usually set up afterwards with set_questions)."""
        root = Path(root)
        _refuse_real_vault_in_tests(root)
        if cls.exists(root):
            raise VaultExistsError(f"A vault already exists at {root}")
        if root.exists() and any(root.iterdir()):
            raise VaultError(f"Folder is not empty: {root}")
        if not password:
            raise ValueError("Password must not be empty")
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(root, 0o700)
        key = random_key()
        params = ScryptParams.generate(kdf_n)
        keyfile = {
            "app": "FaceDictionary",
            "format": FORMAT,
            "kdf": params.to_dict(),
            "wrapped_key": b64e(seal(derive_password_key(password, params), key, b"key:password")),
            "recovery": {"slots": []},
        }
        code = None
        if recovery_code:
            code = new_recovery_code()
            salt = secrets.token_bytes(16)
            keyfile["recovery"]["slots"].append(
                {"type": "code", "salt": b64e(salt),
                 "wrapped_key": b64e(seal(derive_recovery_key(code, salt), key, b"key:recovery"))})
        vault = cls(root, key, keyfile)
        vault._ensure_dirs()
        atomic_write(root / KEYFILE, _dumps(keyfile))
        return vault, code

    @classmethod
    def _read_keyfile(cls, root: Path) -> dict:
        root = Path(root)
        _refuse_real_vault_in_tests(root)
        if not cls.exists(root):
            raise VaultNotFoundError(f"No vault at {root}")
        keyfile = json.loads((root / KEYFILE).read_text("utf-8"))
        if keyfile.get("format") not in SUPPORTED_FORMATS:
            raise UnsupportedVaultError(f"This database was made by a newer version of FaceDictionary "
                                        f"(format {keyfile.get('format')}). Please update the app.")
        return keyfile

    @classmethod
    def open(cls, root: Path, password: str) -> "Vault":
        keyfile = cls._read_keyfile(root)
        params = ScryptParams.from_dict(keyfile["kdf"])
        try:
            key = unseal(derive_password_key(password, params), b64d(keyfile["wrapped_key"]), b"key:password")
        except TamperedError:
            raise WrongPasswordError("Wrong password") from None
        vault = cls(Path(root), key, keyfile)
        vault._ensure_dirs()
        return vault

    @classmethod
    def recover(cls, root: Path, recovery_code: str, new_password: str) -> "Vault":
        """Open with the recovery code and set a new password."""
        keyfile = cls._read_keyfile(root)
        slot = _slot(keyfile, "code")
        if slot is None:
            raise VaultError("This database has no recovery code.")
        try:
            key = unseal(derive_recovery_key(recovery_code, b64d(slot["salt"])), b64d(slot["wrapped_key"]),
                         b"key:recovery")
        except TamperedError:
            raise WrongPasswordError("Wrong recovery code") from None
        return cls._reopen(root, key, keyfile, new_password)

    @classmethod
    def recover_with_answers(cls, root: Path, answers: dict[int, str], new_password: str) -> "Vault":
        """Open with the answers to the security questions and set a new password.
        `answers` maps question index -> answer; unanswered questions can be left out."""
        keyfile = cls._read_keyfile(root)
        slot = _slot(keyfile, "questions")
        if slot is None:
            raise VaultError("This database has no security questions.")
        params = ScryptParams.from_dict(slot["kdf"])
        given = {i: a for i, a in answers.items() if normalize_answer(a)}
        required = int(slot.get("required", 0))
        tried = False
        for combo in slot.get("combos", []):
            idx = tuple(combo["indexes"])
            if not all(i in given for i in idx):
                continue
            tried = True
            try:
                key = unseal(derive_answers_key([given[i] for i in idx], params, idx),
                             b64d(combo["wrapped_key"]), b"key:questions")
            except TamperedError:
                continue
            return cls._reopen(root, key, keyfile, new_password)
        if not tried:
            raise WrongPasswordError(f"Answer at least {required} of the questions.")
        raise WrongPasswordError("Those answers do not match. Case, spaces and punctuation do not matter.")

    @classmethod
    def _reopen(cls, root: Path, key: bytes, keyfile: dict, new_password: str) -> "Vault":
        vault = cls(Path(root), key, keyfile)
        vault._ensure_dirs()
        vault._set_password(new_password)
        return vault

    def set_questions(self, password: str, questions: list[str], answers: list[str], required: int = 2) -> None:
        """Set (or replace) the security questions. Needs the current password."""
        with self._lock:
            self._check_password(password)
            slot = _questions_slot(self._require_key(), questions, answers, required,
                                   ScryptParams.from_dict(self._keyfile["kdf"]).n)
            self._write_keyfile(_with_slot(self._keyfile, "questions", slot))

    def clear_recovery(self, password: str, kind: str) -> None:
        """Remove one recovery method ("questions" or "code")."""
        with self._lock:
            self._check_password(password)
            self._write_keyfile(_with_slot(self._keyfile, kind, None))

    def recovery_info(self) -> VaultInfo:
        return self.inspect(self.root)

    def _write_keyfile(self, keyfile: dict) -> None:
        atomic_write(self.root / KEYFILE, _dumps(keyfile))
        self._keyfile = keyfile

    def change_password(self, old_password: str, new_password: str) -> None:
        with self._lock:
            self._check_password(old_password)
            self._set_password(new_password)

    def new_recovery_code(self, password: str) -> str:
        """Replace the recovery code (the old one stops working). Requires the current password."""
        with self._lock:
            self._check_password(password)
            code = new_recovery_code()
            salt = secrets.token_bytes(16)
            slot = {"type": "code", "salt": b64e(salt),
                    "wrapped_key": b64e(seal(derive_recovery_key(code, salt), self._require_key(),
                                             b"key:recovery"))}
            self._write_keyfile(_with_slot(self._keyfile, "code", slot))
            return code

    def _check_password(self, password: str) -> None:
        params = ScryptParams.from_dict(self._keyfile["kdf"])
        try:
            unseal(derive_password_key(password, params), b64d(self._keyfile["wrapped_key"]), b"key:password")
        except TamperedError:
            raise WrongPasswordError("Wrong password") from None

    def _set_password(self, password: str) -> None:
        if not password:
            raise ValueError("Password must not be empty")
        old = ScryptParams.from_dict(self._keyfile["kdf"])
        params = ScryptParams.generate(old.n)
        keyfile = _with_slot(self._keyfile, "__none__", None)      # also upgrades the format if needed
        keyfile["kdf"] = params.to_dict()
        keyfile["wrapped_key"] = b64e(seal(derive_password_key(password, params), self._require_key(),
                                           b"key:password"))
        self._write_keyfile(keyfile)

    @property
    def locked(self) -> bool:
        return self._key is None

    def lock(self) -> None:
        """Forget the key and all decrypted data. Never touches the disk."""
        with self._lock:
            self._key = None
            self._entries = None
            self._history = None
            self.unreadable = set()

    def _require_key(self) -> bytes:
        if self._key is None:
            raise VaultLockedError("The vault is locked")
        return self._key

    def _ensure_dirs(self) -> None:
        for d in (*KIND_DIRS.values(), TRASH):
            (self.root / d).mkdir(mode=0o700, exist_ok=True)

    # --- low-level encrypted objects ---------------------------------------------

    @staticmethod
    def _check_id(kind: str, oid: str) -> None:
        pattern = _INDEX_ID_RE if kind in ("index", "meta") else _ID_RE
        if not pattern.match(oid):
            raise VaultError(f"Invalid {kind} id")

    @staticmethod
    def _rel(kind: str, oid: str) -> str:
        return f"{KIND_DIRS[kind]}/{oid}.enc"

    def _path(self, kind: str, oid: str) -> Path:
        self._check_id(kind, oid)
        return self.root / self._rel(kind, oid)

    def _put(self, kind: str, oid: str, data: bytes, durable: bool = True) -> None:
        blob = seal(self._require_key(), data, f"{kind}:{oid}".encode())
        atomic_write(self._path(kind, oid), blob, durable=durable)

    def _get(self, kind: str, oid: str) -> bytes:
        key = self._require_key()
        path = self._path(kind, oid)
        try:
            blob = path.read_bytes()
        except FileNotFoundError:
            raise VaultError(f"Missing {kind} {oid}") from None
        return unseal(key, blob, f"{kind}:{oid}".encode())

    def _get_json(self, kind: str, oid: str) -> dict:
        return json.loads(self._get(kind, oid).decode("utf-8"))

    def _ids(self, kind: str) -> list[str]:
        return [p.stem for p in (self.root / KIND_DIRS[kind]).glob("*.enc")]

    def _load_all(self, kind: str, factory) -> dict:
        """Decrypt every object of a kind; a damaged file is skipped and remembered, not fatal."""
        out = {}
        for oid in self._ids(kind):
            try:
                out[oid] = factory(self._get_json(kind, oid))
            except (TamperedError, VaultError, ValueError, KeyError, TypeError):
                if self.locked:
                    raise
                self.unreadable.add(f"{KIND_DIRS[kind]}/{oid}.enc")
        return out

    # --- entries -------------------------------------------------------------------

    def _entry_cache(self) -> dict[str, Entry]:
        self._require_key()
        if self._entries is None:
            self._entries = self._load_all("entry", Entry.from_dict)
        return self._entries

    def list_entries(self) -> list[Entry]:
        with self._lock:
            entries = [Entry.from_dict(e.to_dict()) for e in self._entry_cache().values()]
        return sorted(entries, key=lambda e: (fold(e.name), e.id))

    def has_entry(self, entry_id: str) -> bool:
        with self._lock:
            return entry_id in self._entry_cache()

    def get_entry(self, entry_id: str) -> Entry:
        with self._lock:
            entry = self._entry_cache().get(entry_id)
            if entry is None:
                raise VaultError("Entry not found")
            return Entry.from_dict(entry.to_dict())

    def save_entry(self, entry: Entry) -> Entry:
        with self._lock:
            entry.updated_at = now_iso()
            self._put("entry", entry.id, _dumps(entry.to_dict()))
            self._entry_cache()[entry.id] = Entry.from_dict(entry.to_dict())
            return entry

    def new_entry(self, name: str, **fields) -> Entry:
        return self.save_entry(Entry(id=new_id(), name=name, **fields))

    # --- photos & thumbnails --------------------------------------------------------

    def put_photo(self, data: bytes) -> str:
        with self._lock:
            photo_id = new_id()
            self._put("photo", photo_id, data)
            return photo_id

    def get_photo(self, photo_id: str) -> bytes:
        with self._lock:
            return self._get("photo", photo_id)

    def has_photo(self, photo_id: str) -> bool:
        return self._path("photo", photo_id).is_file()

    def put_thumb(self, photo_id: str, data: bytes) -> None:
        with self._lock:
            self._put("thumb", photo_id, data, durable=False)

    def get_thumb(self, photo_id: str) -> bytes | None:
        """Thumbnails are a cache: missing or unreadable ones return None and get rebuilt."""
        with self._lock:
            try:
                return self._get("thumb", photo_id)
            except (VaultError, TamperedError):
                if self.locked:
                    raise
                return None

    # --- face index cache -----------------------------------------------------------

    def save_index(self, model: str, data: bytes) -> None:
        with self._lock:
            self._put("index", model, data, durable=False)

    def load_index(self, model: str) -> bytes | None:
        with self._lock:
            try:
                return self._get("index", model)
            except (VaultError, TamperedError):
                if self.locked:
                    raise
                return None

    # --- small JSON documents (settings) -------------------------------------------------

    def get_meta(self, name: str) -> dict | None:
        with self._lock:
            if not self._path("meta", name).exists():
                self._require_key()
                return None
            return self._get_json("meta", name)

    def put_meta(self, name: str, doc: dict) -> None:
        with self._lock:
            self._put("meta", name, _dumps(doc))

    # --- global field definitions ---------------------------------------------------------

    def get_field_defs(self) -> list[FieldDef]:
        """The fields shared by every entry of this database, in display order."""
        doc = self.get_meta("fields") or {}
        return [FieldDef.from_dict(f) for f in doc.get("fields", []) if isinstance(f, dict)]

    def set_field_defs(self, defs: list[FieldDef]) -> None:
        self.put_meta("fields", {"version": 1, "fields": [f.to_dict() for f in defs]})

    # --- history --------------------------------------------------------------------

    def _history_cache(self) -> dict[str, HistoryRecord]:
        self._require_key()
        if self._history is None:
            self._history = self._load_all("history", HistoryRecord.from_dict)
        return self._history

    def list_history(self) -> list[HistoryRecord]:
        with self._lock:
            recs = [HistoryRecord.from_dict(r.to_dict()) for r in self._history_cache().values()]
        return sorted(recs, key=lambda r: (r.time, r.id), reverse=True)

    def get_history(self, history_id: str) -> HistoryRecord:
        with self._lock:
            rec = self._history_cache().get(history_id)
            if rec is None:
                raise VaultError("History record not found")
            return HistoryRecord.from_dict(rec.to_dict())

    def save_history(self, record: HistoryRecord) -> HistoryRecord:
        """Store a record; returns it exactly as stored (e.g. coordinates rounded)."""
        with self._lock:
            doc = record.to_dict()
            self._put("history", record.id, _dumps(doc))
            self._history_cache()[record.id] = HistoryRecord.from_dict(doc)
            return HistoryRecord.from_dict(doc)

    # --- trash ------------------------------------------------------------------------

    def _trash_dir(self, trash_id: str) -> Path:
        self._check_id("trash", trash_id)
        return self.root / TRASH / trash_id

    def _begin_trash(self, item: TrashItem, rels: list[str]) -> Path:
        """Create the trash folder and write its manifest before anything is moved."""
        tdir = self._trash_dir(item.id)
        tdir.mkdir(mode=0o700, parents=True)
        item.files = [rel for rel in rels if (self.root / rel).exists()]
        atomic_write(tdir / MANIFEST, seal(self._require_key(), _dumps(item.to_dict()), f"trash:{item.id}".encode()))
        return tdir

    def _finish_trash(self, item: TrashItem, tdir: Path) -> None:
        for rel in item.files:
            dest = tdir / rel
            dest.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            os.replace(self.root / rel, dest)

    def delete_entry(self, entry_id: str) -> TrashItem:
        """Move an entry and all its photos to the trash."""
        with self._lock:
            entry = self.get_entry(entry_id)
            item = TrashItem(id=new_id(), time=now_iso(), kind="entry", label=entry.name, files=[],
                             payload={"entry_id": entry_id, "photo_count": len(entry.photos)})
            rels = [self._rel("entry", entry_id)]           # the entry file moves first
            for p in entry.photos:
                rels += [self._rel("photo", p.photo_id), self._rel("thumb", p.photo_id)]
            tdir = self._begin_trash(item, rels)
            self._finish_trash(item, tdir)
            self._entry_cache().pop(entry_id, None)
            return item

    def remove_photo(self, entry_id: str, photo_id: str) -> TrashItem:
        """Remove one photo from an entry's album (the photo goes to the trash)."""
        with self._lock:
            entry = self.get_entry(entry_id)
            ref = entry.photo(photo_id)
            if ref is None:
                raise VaultError("Photo not found in entry")
            item = TrashItem(id=new_id(), time=now_iso(), kind="photo", label=f"Photo of {entry.name}", files=[],
                             payload={"entry_id": entry_id, "photo_ref": ref.to_dict()})
            tdir = self._begin_trash(item, [self._rel("photo", photo_id), self._rel("thumb", photo_id)])
            entry.photos = [p for p in entry.photos if p.photo_id != photo_id]
            self.save_entry(entry)                           # drop the reference before moving the file
            self._finish_trash(item, tdir)
            return item

    def delete_history(self, history_id: str) -> TrashItem:
        with self._lock:
            rec = self.get_history(history_id)
            item = TrashItem(id=new_id(), time=now_iso(), kind="history",
                             label=f"Search from {rec.time[:16].replace('T', ' ')}", files=[],
                             payload={"history_id": history_id})
            tdir = self._begin_trash(item, [self._rel("history", history_id), self._rel("photo", rec.photo_id),
                                            self._rel("thumb", rec.photo_id)])
            self._finish_trash(item, tdir)
            self._history_cache().pop(history_id, None)
            return item

    def _read_trash(self, trash_id: str) -> TrashItem:
        blob = (self._trash_dir(trash_id) / MANIFEST).read_bytes()
        return TrashItem.from_dict(json.loads(unseal(self._require_key(), blob, f"trash:{trash_id}".encode())))

    def list_trash(self) -> list[TrashItem]:
        with self._lock:
            self._require_key()
            items = []
            for d in (self.root / TRASH).iterdir():
                if d.is_dir() and _ID_RE.match(d.name) and (d / MANIFEST).exists():
                    try:
                        items.append(self._read_trash(d.name))
                    except (TamperedError, ValueError):
                        continue
            return sorted(items, key=lambda i: (i.time, i.id), reverse=True)

    def restore(self, trash_id: str) -> TrashItem:
        with self._lock:
            item = self._read_trash(trash_id)
            tdir = self._trash_dir(trash_id)
            if item.kind == "photo" and not self.has_entry(item.payload["entry_id"]):
                raise RestoreConflictError("The entry this photo belonged to no longer exists. "
                                           "Restore the entry first.")
            for rel in item.files:
                if (tdir / rel).exists() and (self.root / rel).exists():
                    raise RestoreConflictError(f"Cannot restore: {rel} already exists")
            for rel in item.files:
                if (tdir / rel).exists():
                    os.replace(tdir / rel, self.root / rel)
            if item.kind == "photo":
                entry = self.get_entry(item.payload["entry_id"])
                ref = PhotoRef.from_dict(item.payload["photo_ref"])
                if entry.photo(ref.photo_id) is None:
                    entry.photos.append(ref)
                    self.save_entry(entry)
            elif item.kind == "entry":
                eid = item.payload["entry_id"]
                self._entry_cache()[eid] = Entry.from_dict(self._get_json("entry", eid))
            elif item.kind == "history":
                hid = item.payload["history_id"]
                self._history_cache()[hid] = HistoryRecord.from_dict(self._get_json("history", hid))
            self._remove_empty_trash_dir(tdir)
            return item

    def _remove_empty_trash_dir(self, tdir: Path) -> None:
        (tdir / MANIFEST).unlink()
        for d in sorted((p for p in tdir.rglob("*") if p.is_dir()), key=lambda p: len(p.parts), reverse=True):
            d.rmdir()                                        # fails loudly if anything is left
        tdir.rmdir()

    def purge(self, trash_id: str) -> None:
        """Permanently delete one trash item. The only operation that destroys data."""
        with self._lock:
            self._require_key()
            tdir = self._trash_dir(trash_id)
            trash_root = (self.root / TRASH).resolve()
            if tdir.resolve().parent != trash_root or not (tdir / MANIFEST).exists():
                raise VaultError("Not a trash item")
            for f in sorted(tdir.rglob("*"), key=lambda p: len(p.parts), reverse=True):
                if trash_root not in f.resolve().parents:
                    raise VaultError("Refusing to delete outside the trash")
                if f.is_file():
                    f.unlink()
                else:
                    f.rmdir()
            tdir.rmdir()

    # --- maintenance --------------------------------------------------------------------

    def verify(self) -> list[str]:
        """Decrypt every object; return a list of problems (empty when everything is intact)."""
        problems = []
        with self._lock:
            for kind, sub in KIND_DIRS.items():
                for path in (self.root / sub).glob("*.enc"):
                    try:
                        self._get(kind, path.stem)
                    except (VaultError, TamperedError) as exc:
                        problems.append(f"{sub}/{path.name}: {exc}")
        return problems

    def stats(self) -> dict[str, int]:
        return {sub: len(list((self.root / sub).glob("*.enc"))) for sub in KIND_DIRS.values()}
