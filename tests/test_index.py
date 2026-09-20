"""M2: face index - per-entry aggregation, updates, serialization."""

from __future__ import annotations

import numpy as np
import pytest

from facedict.index import FaceIndex


def unit(*xs) -> np.ndarray:
    v = np.array(xs, dtype=np.float32)
    return v / np.linalg.norm(v)


@pytest.fixture
def idx() -> FaceIndex:
    i = FaceIndex("test", 3)
    i.add_many([("p1", "A", unit(1, 0, 0)), ("p2", "A", unit(0.6, 0.8, 0)),
                ("p3", "B", unit(0, 1, 0)), ("p4", "C", unit(0, 0, 1)), ("p5", "B", unit(0.9, 0.1, 0))])
    return i


def test_search_ranks_entries_by_best_photo(idx):
    hits = idx.search(unit(1, 0.05, 0), k=5)
    assert [h.entry_id for h in hits] == ["A", "B", "C"]
    assert hits[0].photo_id == "p1" and hits[1].photo_id == "p5"
    assert hits[0].score >= hits[1].score >= hits[2].score


def test_k_and_exclude(idx):
    assert len(idx.search(unit(1, 0, 0), k=2)) == 2
    hits = idx.search(unit(1, 0, 0), k=5, exclude_photo="p1")
    assert hits[0].entry_id == "B" and hits[0].photo_id == "p5"     # p5 (B) beats p2 (A) now


def test_add_replace_remove(idx):
    idx.add("p4", "A", unit(1, 0, 0))                 # move p4 to entry A with a new vector
    assert len(idx) == 5 and idx.entry_of("p4") == "A"
    assert idx.remove(["p4", "nope"]) == 1
    assert "p4" not in idx and len(idx) == 4
    assert idx.remove_entry("B") == 2
    assert {h.entry_id for h in idx.search(unit(0, 1, 0))} == {"A"}
    with pytest.raises(ValueError):
        idx.add("x", "A", np.ones(4))


def test_serialization_roundtrip(idx):
    back = FaceIndex.from_bytes(idx.to_bytes())
    assert back.model == "test" and back.dim == 3
    assert back.photo_ids == idx.photo_ids and back.entry_ids == idx.entry_ids
    assert np.array_equal(back.vectors, idx.vectors)
    q = unit(0.3, 0.3, 0.9)
    assert [(h.entry_id, h.photo_id) for h in back.search(q)] == [(h.entry_id, h.photo_id) for h in idx.search(q)]


def test_empty_index():
    i = FaceIndex("m", 4)
    assert i.search(np.ones(4)) == []
    assert FaceIndex.from_bytes(i.to_bytes()).search(np.ones(4)) == []
