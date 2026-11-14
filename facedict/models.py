"""Plain data records stored (encrypted) in the vault."""

from __future__ import annotations

import base64
import unicodedata
import uuid
from dataclasses import dataclass, field
from datetime import datetime

import numpy as np


def fold(text: str) -> str:
    """Normalize text for searching, sorting and comparing names, in any script:
    full-width and half-width forms, composed and decomposed accents, and case all fold together."""
    return unicodedata.normalize("NFKC", text or "").casefold().strip()


def split_values(text: str) -> list[str]:
    """Split a comma-separated line; Chinese and Japanese commas count too."""
    for sep in ("，", "、", "；", ";"):
        text = text.replace(sep, ",")
    return [part.strip() for part in text.split(",") if part.strip()]


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def new_id() -> str:
    return uuid.uuid4().hex


@dataclass
class Face:
    """A detected face in original-image pixel coordinates."""
    bbox: list[float]                  # x, y, w, h
    landmarks: list[list[float]]       # 5 x (x, y): eyes, nose tip, mouth corners
    score: float

    @property
    def area(self) -> float:
        return self.bbox[2] * self.bbox[3]

    def to_dict(self) -> dict:
        return {"bbox": [round(v, 2) for v in self.bbox],
                "landmarks": [[round(x, 2), round(y, 2)] for x, y in self.landmarks],
                "score": round(float(self.score), 4)}

    @classmethod
    def from_dict(cls, d: dict | None) -> "Face | None":
        if not d:
            return None
        return cls(bbox=[float(v) for v in d["bbox"]],
                   landmarks=[[float(x), float(y)] for x, y in d["landmarks"]],
                   score=float(d.get("score", 0.0)))


@dataclass
class PhotoRef:
    photo_id: str
    sha256: str
    face: Face | None
    face_count: int = 1                # faces detected when the photo was added
    source: str = "import"             # "import" | "query:<history_id>#<face index>"
    added_at: str = field(default_factory=now_iso)
    note: str = ""                     # the user's own words about this photo

    @property
    def from_query(self) -> str | None:
        """History id of the search this photo was added from, if any."""
        if not self.source.startswith("query:"):
            return None
        return self.source.split(":", 1)[1].split("#", 1)[0]

    @property
    def query_face(self) -> int:
        """Which face of that search (0 for photos added by the first version)."""
        tail = self.source.split("#", 1)
        return int(tail[1]) if len(tail) == 2 and tail[1].isdigit() else 0

    def to_dict(self) -> dict:
        return {"photo_id": self.photo_id, "sha256": self.sha256,
                "face": self.face.to_dict() if self.face else None,
                "face_count": self.face_count, "source": self.source, "added_at": self.added_at,
                "note": self.note}

    @classmethod
    def from_dict(cls, d: dict) -> "PhotoRef":
        return cls(photo_id=d["photo_id"], sha256=d["sha256"], face=Face.from_dict(d.get("face")),
                   face_count=int(d.get("face_count", 1)), source=d.get("source", "import"),
                   added_at=d.get("added_at", now_iso()), note=str(d.get("note", "")))


@dataclass
class FieldDef:
    """A global field: it exists on every entry of the database (each entry has its own text)."""
    id: str
    name: str

    def to_dict(self) -> dict:
        return {"id": self.id, "name": self.name}

    @classmethod
    def from_dict(cls, d: dict) -> "FieldDef":
        return cls(id=d["id"], name=d.get("name", ""))


@dataclass
class OwnField:
    """A field that belongs to one entry only."""
    name: str
    value: str = ""

    def to_dict(self) -> dict:
        return {"name": self.name, "value": self.value}

    @classmethod
    def from_dict(cls, d: dict) -> "OwnField":
        return cls(name=d.get("name", ""), value=d.get("value", ""))


@dataclass
class Entry:
    id: str
    name: str
    aliases: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    fields: dict[str, str] = field(default_factory=dict)     # global field id -> text
    own: list[OwnField] = field(default_factory=list)        # fields of this entry only
    photos: list[PhotoRef] = field(default_factory=list)     # the first one is the entry's cover
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)
    legacy_notes: str = ""                                   # from the first version, until migrated
    legacy_extra: dict = field(default_factory=dict)

    CORE_KEYS = ("id", "name", "aliases", "tags", "notes", "photos", "created_at", "updated_at", "fields", "own")

    def photo(self, photo_id: str) -> PhotoRef | None:
        return next((p for p in self.photos if p.photo_id == photo_id), None)

    @property
    def cover(self) -> PhotoRef | None:
        return self.photos[0] if self.photos else None

    @property
    def needs_migration(self) -> bool:
        return bool(self.legacy_notes or self.legacy_extra)

    def own_field(self, name: str) -> OwnField | None:
        key = fold(name)
        return next((f for f in self.own if fold(f.name) == key), None)

    def to_dict(self) -> dict:
        doc = {"id": self.id, "name": self.name, "aliases": list(self.aliases), "tags": list(self.tags),
               "fields": {k: v for k, v in self.fields.items()}, "own": [f.to_dict() for f in self.own],
               "photos": [p.to_dict() for p in self.photos],
               "created_at": self.created_at, "updated_at": self.updated_at}
        if self.legacy_notes:                                # keep until the migration has run
            doc["notes"] = self.legacy_notes
        if self.legacy_extra:
            doc["extra"] = dict(self.legacy_extra)
        return doc

    @classmethod
    def from_dict(cls, d: dict) -> "Entry":
        return cls(id=d["id"], name=d.get("name", ""), aliases=list(d.get("aliases", [])),
                   tags=list(d.get("tags", [])),
                   fields={str(k): str(v) for k, v in (d.get("fields") or {}).items()},
                   own=[OwnField.from_dict(f) for f in d.get("own", [])],
                   photos=[PhotoRef.from_dict(p) for p in d.get("photos", [])],
                   created_at=d.get("created_at", now_iso()), updated_at=d.get("updated_at", now_iso()),
                   legacy_notes=str(d.get("notes", "")), legacy_extra=dict(d.get("extra") or {}))


@dataclass
class Match:
    rank: int
    entry_id: str
    name_snapshot: str
    score: float
    best_photo_id: str | None

    def to_dict(self) -> dict:
        return {"rank": self.rank, "entry_id": self.entry_id, "name_snapshot": self.name_snapshot,
                "score": round(float(self.score), 5), "best_photo_id": self.best_photo_id}

    @classmethod
    def from_dict(cls, d: dict) -> "Match":
        return cls(rank=int(d["rank"]), entry_id=d["entry_id"], name_snapshot=d.get("name_snapshot", ""),
                   score=float(d["score"]), best_photo_id=d.get("best_photo_id"))


@dataclass
class Confirmation:
    entry_id: str
    added_photo_id: str
    time: str = field(default_factory=now_iso)

    def to_dict(self) -> dict:
        return {"entry_id": self.entry_id, "added_photo_id": self.added_photo_id, "time": self.time}

    @classmethod
    def from_dict(cls, d: dict | None) -> "Confirmation | None":
        return cls(**d) if d else None


def _encode_vec(v: np.ndarray | None) -> str | None:
    if v is None:
        return None
    return base64.b64encode(np.asarray(v, dtype="<f4").tobytes()).decode("ascii")


def _decode_vec(text: str | None) -> np.ndarray | None:
    if not text:
        return None
    return np.frombuffer(base64.b64decode(text), dtype="<f4").astype(np.float32)


@dataclass
class FaceSearch:
    """One detected face of a searched photo and its stored top-k result."""
    face: Face
    embedding: np.ndarray | None
    matches: list[Match]
    confirmed: Confirmation | None = None

    def to_dict(self) -> dict:
        return {"face": self.face.to_dict(), "embedding": _encode_vec(self.embedding),
                "matches": [m.to_dict() for m in self.matches],
                "confirmed": self.confirmed.to_dict() if self.confirmed else None}

    @classmethod
    def from_dict(cls, d: dict) -> "FaceSearch":
        return cls(face=Face.from_dict(d["face"]), embedding=_decode_vec(d.get("embedding")),
                   matches=[Match.from_dict(m) for m in d.get("matches", [])],
                   confirmed=Confirmation.from_dict(d.get("confirmed")))


@dataclass
class HistoryRecord:
    """A searched photo: every face found in it (in reading order) with its own result."""
    id: str
    time: str
    photo_id: str
    model: str
    searches: list[FaceSearch]
    selected: int = 0                  # the face shown by default (largest, or the one last looked at)

    @property
    def face_count(self) -> int:
        return len(self.searches)

    def search(self, index: int | None = None) -> FaceSearch | None:
        i = self.selected if index is None else index
        return self.searches[i] if 0 <= i < len(self.searches) else None

    # the selected face's data, for code that only cares about one face
    @property
    def face(self) -> Face | None:
        s = self.search()
        return s.face if s else None

    @property
    def matches(self) -> list[Match]:
        s = self.search()
        return s.matches if s else []

    @property
    def embedding(self) -> np.ndarray | None:
        s = self.search()
        return s.embedding if s else None

    @property
    def confirmed(self) -> Confirmation | None:
        s = self.search()
        return s.confirmed if s else None

    @property
    def any_confirmed(self) -> bool:
        return any(s.confirmed for s in self.searches)

    def to_dict(self) -> dict:
        return {"id": self.id, "time": self.time, "photo_id": self.photo_id, "model": self.model,
                "searches": [s.to_dict() for s in self.searches], "selected": self.selected}

    @classmethod
    def from_dict(cls, d: dict) -> "HistoryRecord":
        if "searches" in d:
            searches = [FaceSearch.from_dict(x) for x in d["searches"]]
            selected = int(d.get("selected", 0))
        else:                                        # format of the first version: one face per record
            face = Face.from_dict(d.get("face"))
            searches = [] if face is None else [FaceSearch(
                face=face, embedding=_decode_vec(d.get("embedding")),
                matches=[Match.from_dict(m) for m in d.get("matches", [])],
                confirmed=Confirmation.from_dict(d.get("confirmed")))]
            selected = 0
        return cls(id=d["id"], time=d["time"], photo_id=d["photo_id"], model=d.get("model", ""),
                   searches=searches, selected=min(max(selected, 0), max(len(searches) - 1, 0)))


@dataclass
class TrashItem:
    id: str
    time: str
    kind: str                          # "entry" | "photo" | "history"
    label: str                         # human-readable description (encrypted like everything else)
    files: list[str]                   # vault-relative paths moved into the trash folder
    payload: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"id": self.id, "time": self.time, "kind": self.kind, "label": self.label,
                "files": list(self.files), "payload": self.payload}

    @classmethod
    def from_dict(cls, d: dict) -> "TrashItem":
        return cls(id=d["id"], time=d["time"], kind=d["kind"], label=d.get("label", ""),
                   files=list(d.get("files", [])), payload=dict(d.get("payload", {})))
