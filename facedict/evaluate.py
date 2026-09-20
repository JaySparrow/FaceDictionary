"""Leave-one-out accuracy of the face recognizers.

Every photo of a person with at least two usable photos is used once as the query; it is removed
from the gallery, and we check whether the correct person ranks first / in the top 5
(people with a single photo remain in the gallery as distractors).

Read-only: nothing is written to the vault. Only aggregate numbers are printed - no names, no paths.

    python -m facedict evaluate                     # your vault (asks for the password)
    python -m facedict evaluate --folder DIR        # a plain folder of <person>/<photos>, e.g. public test data
"""

from __future__ import annotations

import argparse
import getpass
import sys
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from . import config
from .engine import FaceEngine
from .imageio import ImageDecodeError, decode, is_supported
from .vault import Vault


@dataclass
class EvalResult:
    model: str
    people: int                 # people used as queries (>= 2 usable photos)
    gallery: int                # photos in the gallery
    queries: int
    top1: float
    top5: float
    ms_per_face: float

    def row(self) -> str:
        return (f"{self.model:<14} {self.people:>7} {self.queries:>8} {self.top1:>8.1%} {self.top5:>8.1%}"
                f" {self.ms_per_face:>9.1f}")


HEADER = f"{'model':<14} {'people':>7} {'queries':>8} {'top-1':>8} {'top-5':>8} {'ms/face':>9}"


def leave_one_out(vectors: np.ndarray, labels: list[str], k: int = 5, block: int = 512) -> tuple[int, int, int]:
    """Returns (queries, top1_hits, topk_hits)."""
    uniq, codes = np.unique(np.asarray(labels), return_inverse=True)
    counts = np.bincount(codes, minlength=len(uniq))
    order = np.argsort(codes, kind="stable")
    sorted_codes = codes[order]
    starts = np.flatnonzero(np.r_[True, sorted_codes[1:] != sorted_codes[:-1]])
    v_sorted = vectors[order]
    queries = top1 = topk = 0
    q_idx = np.flatnonzero(counts[codes] >= 2)
    pos_in_sorted = np.empty_like(order)
    pos_in_sorted[order] = np.arange(len(order))
    for b in range(0, len(q_idx), block):
        qs = q_idx[b:b + block]
        sims = vectors[qs] @ v_sorted.T
        sims[np.arange(len(qs)), pos_in_sorted[qs]] = -np.inf           # leave the query out
        per_person = np.maximum.reduceat(sims, starts, axis=1)          # best photo per person
        true_scores = per_person[np.arange(len(qs)), codes[qs]]
        ranks = (per_person > true_scores[:, None]).sum(axis=1)
        queries += len(qs)
        top1 += int((ranks == 0).sum())
        topk += int((ranks < k).sum())
    return queries, top1, topk


Sample = tuple[str, np.ndarray]           # (person label, aligned 112x112 face) - alignment is model-independent


def _evaluate(model: str, samples: list[Sample]) -> EvalResult:
    eng = FaceEngine(model)
    vecs, labels = [], []
    t0 = time.perf_counter()
    for label, aligned in samples:
        vecs.append(eng.embed_aligned(aligned))
        labels.append(label)
    ms = (time.perf_counter() - t0) * 1000 / max(1, len(samples))
    queries, t1, t5 = leave_one_out(np.stack(vecs), labels)
    people = sum(1 for n in Counter(labels).values() if n >= 2)
    return EvalResult(model, people, len(labels), queries, t1 / max(1, queries), t5 / max(1, queries), ms)


def samples_from_vault(vault: Vault) -> list[Sample]:
    out = []
    for entry in vault.list_entries():
        for ref in entry.photos:
            if ref.face is None:
                continue
            try:
                out.append((entry.id, FaceEngine.align(decode(vault.get_photo(ref.photo_id)), ref.face)))
            except ImageDecodeError:
                continue
    return out


def samples_from_folder(root: Path) -> tuple[list[Sample], int]:
    eng = FaceEngine(config.DEFAULT_RECOGNIZER)
    out, no_face = [], 0
    for person in sorted(d for d in Path(root).iterdir() if d.is_dir()):
        for f in sorted(p for p in person.iterdir() if p.is_file() and is_supported(p)):
            try:
                img = decode(f.read_bytes())
            except ImageDecodeError:
                continue
            faces = eng.detect(img)
            if not faces:
                no_face += 1
                continue
            out.append((person.name, FaceEngine.align(img, faces[0])))
    return out, no_face


def run(samples, models: list[str], out=sys.stdout) -> list[EvalResult]:
    results = []
    print(HEADER, file=out)
    for m in models:
        r = _evaluate(m, samples)
        results.append(r)
        print(r.row(), file=out, flush=True)
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="facedict evaluate", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--folder", type=Path, help="evaluate a plain folder instead of the vault")
    parser.add_argument("--vault", type=Path, help="vault location (default: the app's vault)")
    parser.add_argument("--models", default=None, help="comma-separated recognizers (default: all installed)")
    parser.add_argument("--password-stdin", action="store_true", help="read the password from stdin")
    args = parser.parse_args(argv)

    installed = [n for n in config.recognizer_names() if (config.models_dir() / config.MODELS[n].filename).exists()]
    models = args.models.split(",") if args.models else installed
    unknown = [m for m in models if m not in installed]
    if unknown:
        parser.error(f"not installed: {', '.join(unknown)} (available: {', '.join(installed)})")

    if args.folder:
        samples, no_face = samples_from_folder(args.folder)
        print(f"photos with a detected face: {len(samples)} (no face found: {no_face})")
    else:
        root = args.vault or config.vault_dir()
        password = sys.stdin.readline().rstrip("\n") if args.password_stdin else getpass.getpass("Vault password: ")
        vault = Vault.open(root, password)
        try:
            samples = samples_from_vault(vault)
        finally:
            vault.lock()
        print(f"library photos with a face: {len(samples)}")
    if len({s[0] for s in samples}) < 2:
        print("Need at least two people with photos to evaluate.")
        return 1
    run(samples, models)
    return 0


if __name__ == "__main__":
    sys.exit(main())
