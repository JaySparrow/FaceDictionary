"""M3: leave-one-out evaluation and the command line."""

from __future__ import annotations

import hashlib
import io
import subprocess
import sys

import numpy as np
import pytest

from facedict import evaluate
from facedict.service import FaceDictionaryService
from tests.conftest import LFW_SUBSET, ROOT, lfw_people


def test_leave_one_out_on_synthetic_vectors():
    rng = np.random.default_rng(0)
    centers = rng.normal(size=(4, 16))
    vecs, labels = [], []
    for c in range(4):
        for _ in range(3):
            v = centers[c] + 0.05 * rng.normal(size=16)
            vecs.append(v / np.linalg.norm(v))
            labels.append(f"p{c}")
    solo = rng.normal(size=16)                        # a person with one photo: distractor only
    vecs.append(solo / np.linalg.norm(solo))
    labels.append("solo")
    q, t1, t5 = evaluate.leave_one_out(np.array(vecs, dtype=np.float32), labels, block=5)
    assert (q, t1, t5) == (12, 12, 12)


def test_leave_one_out_counts_misses():
    # two people whose photos are swapped: every query ranks the wrong person first
    vecs = np.array([[1, 0], [0, 1], [0, 1], [1, 0]], dtype=np.float32)
    q, t1, t5 = evaluate.leave_one_out(vecs, ["a", "a", "b", "b"], k=1)
    assert (q, t1, t5) == (4, 0, 0)


def test_folder_evaluation_on_lfw(models_available):
    lfw_people()                                      # skips if test data is missing
    samples, no_face = evaluate.samples_from_folder(LFW_SUBSET)
    assert len(samples) > 400 and no_face < 10
    out = io.StringIO()
    results = evaluate.run(samples, ["arcface_mbf", "sface"], out=out)
    by_model = {r.model: r for r in results}
    assert by_model["arcface_mbf"].top1 > 0.9 and by_model["arcface_mbf"].top5 > 0.95
    assert by_model["sface"].top1 > 0.8
    print(out.getvalue())


def test_vault_evaluation_is_read_only_and_prints_no_names(isolated_vault, models_available, tmp_path):
    people = dict(list(lfw_people(min_images=4).items())[:6])
    src = tmp_path / "src"
    for name, imgs in people.items():
        (src / name).mkdir(parents=True)
        for p in imgs[:4]:
            (src / name / p.name).write_bytes(p.read_bytes())
    svc = FaceDictionaryService(isolated_vault)
    svc.create_vault("pw", kdf_n=2 ** 12)
    svc.import_folder(src)
    svc.lock()
    before = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in isolated_vault.rglob("*") if p.is_file()}
    proc = subprocess.run([sys.executable, "-m", "facedict", "evaluate", "--vault", str(isolated_vault),
                           "--models", "arcface_mbf", "--password-stdin"],
                          input="pw\n", capture_output=True, text=True, cwd=ROOT, timeout=300)
    assert proc.returncode == 0, proc.stderr
    after = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in isolated_vault.rglob("*") if p.is_file()}
    assert after == before
    for name in people:
        assert name not in proc.stdout and name.replace("_", " ") not in proc.stdout
    assert "arcface_mbf" in proc.stdout and "%" in proc.stdout


def test_cli_query_and_wrong_password(isolated_vault, models_available, tmp_path):
    people = dict(list(lfw_people(min_images=4).items())[:3])
    src = tmp_path / "src"
    for name, imgs in people.items():
        (src / name).mkdir(parents=True)
        for p in imgs[:3]:
            (src / name / p.name).write_bytes(p.read_bytes())
    svc = FaceDictionaryService(isolated_vault)
    svc.create_vault("pw", kdf_n=2 ** 12)
    svc.import_folder(src)
    svc.lock()
    name, imgs = next(iter(people.items()))
    cmd = [sys.executable, "-m", "facedict", "--vault", str(isolated_vault), "query", str(imgs[3]),
           "--no-history", "--password-stdin"]
    ok = subprocess.run(cmd, input="pw\n", capture_output=True, text=True, cwd=ROOT, timeout=120)
    assert ok.returncode == 0, ok.stderr
    assert ok.stdout.splitlines()[1].strip().startswith("1. " + name.replace("_", " "))
    bad = subprocess.run(cmd, input="nope\n", capture_output=True, text=True, cwd=ROOT, timeout=120)
    assert bad.returncode != 0 and "Wrong password" in bad.stderr


@pytest.mark.parametrize("sub", ["verify"])
def test_cli_verify(isolated_vault, sub):
    svc = FaceDictionaryService(isolated_vault)
    svc.create_vault("pw", kdf_n=2 ** 12)
    svc.create_entry("Someone")
    svc.lock()
    proc = subprocess.run([sys.executable, "-m", "facedict", "--vault", str(isolated_vault), sub, "--password-stdin"],
                          input="pw\n", capture_output=True, text=True, cwd=ROOT, timeout=60)
    assert proc.returncode == 0 and "0 problem(s)" in proc.stdout
