"""M1: no plaintext personal data may appear on disk - neither in the vault nor anywhere else."""

from __future__ import annotations

import hashlib
import os
import tempfile
import time
from pathlib import Path

import numpy as np

from facedict.models import Face, FaceSearch, HistoryRecord, Match, OwnField, PhotoRef, new_id, now_iso
from facedict.vault import Vault
from tests.conftest import LFW_SUBSET

CANARY = "CANARY-7f3a91-张三-Zhang"            # includes CJK to catch encoding slips
JPEG_MAGIC = b"\xff\xd8\xff"


def fake_jpeg() -> bytes:
    real = sorted(LFW_SUBSET.rglob("*.jpg"))
    if real:
        return real[0].read_bytes()
    return JPEG_MAGIC + b"\xe0\x00\x10JFIF\x00" + os.urandom(2000)


def needles(photo: bytes) -> list[bytes]:
    # multi-byte needles only: short ones (e.g. the 3-byte JPEG magic) occur in random ciphertext by chance
    return [CANARY.encode("utf-8"), CANARY.encode("utf-16-le"), b"CANARY-7f3a91",
            photo[:16], b"JFIF\x00", photo[200:232]]


def files_modified_since(roots: list[Path], since: float, max_depth: int = 6):
    for root in roots:
        if not root.exists():
            continue
        base_depth = len(root.parts)
        for dirpath, dirnames, filenames in os.walk(root, onerror=lambda e: None):
            if len(Path(dirpath).parts) - base_depth >= max_depth:
                dirnames.clear()
            for name in filenames:
                p = Path(dirpath) / name
                try:
                    st = p.stat()
                except OSError:
                    continue
                if st.st_mtime >= since and st.st_size < 50_000_000:
                    yield p


def test_no_plaintext_in_vault_or_system_locations(isolated_vault, tmp_path_factory):
    start = time.time()
    test_inputs = tmp_path_factory.getbasetemp()          # other tests' plaintext inputs live here
    photo = fake_jpeg()
    vault, _ = Vault.create(isolated_vault, "pw", kdf_n=2 ** 12)
    entry = vault.new_entry(CANARY, aliases=[CANARY], tags=[CANARY],
                            own=[OwnField(CANARY, f"lives in {CANARY}")])
    pid = vault.put_photo(photo)
    vault.put_thumb(pid, photo)
    entry.photos.append(PhotoRef(photo_id=pid, sha256=hashlib.sha256(photo).hexdigest(),
                                 face=Face([0, 0, 1, 1], [[0, 0]] * 5, 1.0)))
    vault.save_entry(entry)
    hpid = vault.put_photo(photo)
    face = Face([0, 0, 1, 1], [[0, 0]] * 5, 1.0)
    vault.save_history(HistoryRecord(id=new_id(), time=now_iso(), photo_id=hpid, model="arcface_mbf", searches=[
        FaceSearch(face, np.ones(8, np.float32), [Match(1, entry.id, CANARY, 0.9, pid)]),
        FaceSearch(face, np.ones(8, np.float32), [Match(1, entry.id, CANARY + "-2", 0.8, pid)])]))
    vault.save_index("arcface_mbf", CANARY.encode() + photo)
    other = vault.new_entry(CANARY + "-trash")
    vault.delete_entry(other.id)                       # trash manifest carries the label

    patterns = needles(photo)
    vault_files = [p for p in isolated_vault.rglob("*") if p.is_file()]
    assert len(vault_files) >= 8
    for p in vault_files:
        data = p.read_bytes()
        hits = [n for n in patterns if n in data]
        assert not hits, f"plaintext {hits} found in {p.relative_to(isolated_vault)}"

    home = Path.home()
    places = [Path(tempfile.gettempdir()), home / "Library" / "Preferences",
              home / "Library" / "Saved Application State"]
    places += [p for p in (home / "Library" / "Caches").glob("*")
               if any(k in p.name.lower() for k in ("python", "qt", "facedict", "pyside"))]
    leaks = []
    for p in files_modified_since(places, start):
        if isolated_vault in p.parents or test_inputs in p.parents:
            continue
        try:
            data = p.read_bytes()
        except OSError:
            continue
        if any(n in data for n in (CANARY.encode("utf-8"), CANARY.encode("utf-16-le"), photo[200:232])):
            leaks.append(p)
    assert not leaks, f"plaintext leaked to {leaks}"
