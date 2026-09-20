"""In-memory face index: one L2-normalized embedding per library photo, searched by cosine similarity."""

from __future__ import annotations

import io
from dataclasses import dataclass
from typing import Iterable

import numpy as np


@dataclass
class Hit:
    entry_id: str
    score: float
    photo_id: str


class FaceIndex:
    def __init__(self, model: str, dim: int):
        self.model = model
        self.dim = dim
        self.photo_ids: list[str] = []
        self.entry_ids: list[str] = []
        self.vectors = np.zeros((0, dim), dtype=np.float32)
        self._pos: dict[str, int] = {}

    def __len__(self) -> int:
        return len(self.photo_ids)

    def __contains__(self, photo_id: str) -> bool:
        return photo_id in self._pos

    def entry_of(self, photo_id: str) -> str | None:
        i = self._pos.get(photo_id)
        return None if i is None else self.entry_ids[i]

    def vector(self, photo_id: str) -> np.ndarray | None:
        i = self._pos.get(photo_id)
        return None if i is None else self.vectors[i].copy()

    def add(self, photo_id: str, entry_id: str, vector: np.ndarray) -> None:
        self.add_many([(photo_id, entry_id, vector)])

    def add_many(self, items: Iterable[tuple[str, str, np.ndarray]]) -> None:
        new_rows = []
        for photo_id, entry_id, vector in items:
            v = np.asarray(vector, dtype=np.float32).reshape(-1)
            if v.shape[0] != self.dim:
                raise ValueError(f"Expected a {self.dim}-d vector, got {v.shape[0]}")
            if photo_id in self._pos:                      # replace in place
                i = self._pos[photo_id]
                self.vectors[i] = v
                self.entry_ids[i] = entry_id
                continue
            self._pos[photo_id] = len(self.photo_ids) + len(new_rows)
            new_rows.append((photo_id, entry_id, v))
        if new_rows:
            self.photo_ids += [r[0] for r in new_rows]
            self.entry_ids += [r[1] for r in new_rows]
            self.vectors = np.vstack([self.vectors, np.stack([r[2] for r in new_rows])])

    def remove(self, photo_ids: Iterable[str]) -> int:
        drop = {p for p in photo_ids if p in self._pos}
        if not drop:
            return 0
        keep = [i for i, p in enumerate(self.photo_ids) if p not in drop]
        self.photo_ids = [self.photo_ids[i] for i in keep]
        self.entry_ids = [self.entry_ids[i] for i in keep]
        self.vectors = self.vectors[keep] if keep else np.zeros((0, self.dim), dtype=np.float32)
        self._pos = {p: i for i, p in enumerate(self.photo_ids)}
        return len(drop)

    def remove_entry(self, entry_id: str) -> int:
        return self.remove([p for p, e in zip(self.photo_ids, self.entry_ids) if e == entry_id])

    def search(self, query: np.ndarray, k: int = 5, exclude_photo: str | None = None) -> list[Hit]:
        """Top-k entries; an entry's score is the best similarity over all of its photos."""
        if not self.photo_ids:
            return []
        sims = self.vectors @ np.asarray(query, dtype=np.float32).reshape(-1)
        if exclude_photo is not None and exclude_photo in self._pos:
            sims[self._pos[exclude_photo]] = -np.inf
        hits: list[Hit] = []
        seen: set[str] = set()
        for i in np.argsort(-sims, kind="stable"):
            if not np.isfinite(sims[i]):
                break
            entry = self.entry_ids[i]
            if entry in seen:
                continue
            seen.add(entry)
            hits.append(Hit(entry, float(sims[i]), self.photo_ids[i]))
            if len(hits) == k:
                break
        return hits

    # --- persistence (bytes are encrypted by the vault) -------------------------------

    def to_bytes(self) -> bytes:
        buf = io.BytesIO()
        np.savez(buf, model=np.array(self.model), dim=np.array(self.dim), vectors=self.vectors,
                 photo_ids=np.array(self.photo_ids, dtype="U32"), entry_ids=np.array(self.entry_ids, dtype="U32"))
        return buf.getvalue()

    @classmethod
    def from_bytes(cls, data: bytes) -> "FaceIndex":
        with np.load(io.BytesIO(data), allow_pickle=False) as z:
            idx = cls(str(z["model"]), int(z["dim"]))
            idx.add_many(zip(z["photo_ids"].tolist(), z["entry_ids"].tolist(), z["vectors"]))
        return idx
