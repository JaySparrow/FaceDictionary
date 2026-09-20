"""FaceDictionary entry point.

    python -m facedict                     start the app (GUI)
    python -m facedict query PHOTO         search from the command line
    python -m facedict import FOLDER       import <person>/ folders into the vault
    python -m facedict evaluate [...]      leave-one-out accuracy (aggregate numbers only)
    python -m facedict verify              check that every vault file decrypts and is intact

The network is switched off (kernel sandbox + in-process guard) before anything else runs.
"""

from __future__ import annotations

from . import netguard

netguard.seal()          # the kernel refuses network calls from here on, plus a guard in Python

import argparse  # noqa: E402
import getpass  # noqa: E402
import sys  # noqa: E402
from pathlib import Path  # noqa: E402


def _password(args) -> str:
    if getattr(args, "password_stdin", False):
        return sys.stdin.readline().rstrip("\n")
    return getpass.getpass("Vault password: ")


def _service(args):
    from .service import FaceDictionaryService

    svc = FaceDictionaryService(args.vault)
    if not svc.vault_exists():
        sys.exit(f"No vault at {svc.vault_root}. Start the app once to create it.")
    svc.unlock(_password(args))
    return svc


def cmd_query(args) -> int:
    svc = _service(args)
    try:
        res = svc.query(Path(args.photo).read_bytes(), save_history=not args.no_history)
        if not res.searches:
            print("No face found in the photo.")
            return 1
        n = len(res.searches)
        index = res.selected if args.face is None else args.face - 1
        if not 0 <= index < n:
            print(f"The photo has {n} face(s); --face must be between 1 and {n}.")
            return 1
        which = "the largest" if args.face is None else "as requested"
        print(f"{n} face(s) found (numbered top-to-bottom, left-to-right); face {index + 1} is {which}. "
              f"{res.elapsed_ms:.0f} ms")
        for m in res.searches[index].matches:
            print(f"  {m.rank}. {m.name_snapshot:<40} {m.score:.3f}")
        if res.low_confidence_for(index):
            print("  (low confidence)")
        if res.history is not None and index != res.selected:
            svc.set_selected(res.history.id, index)
        return 0
    finally:
        svc.lock()


def cmd_import(args) -> int:
    svc = _service(args)
    try:
        def progress(done, total):
            print(f"\r  {done}/{total} photos", end="", flush=True)

        rep = svc.import_folder(Path(args.folder), progress=progress)
        print()
        print(f"entries created: {rep.entries_created}, updated: {rep.entries_updated}, "
              f"photos added: {rep.photos_added}, duplicates skipped: {rep.duplicates}, "
              f"without a face: {rep.no_face}, with several faces: {rep.multi_face}, errors: {len(rep.errors)}")
        return 0 if not rep.errors else 2
    finally:
        svc.lock()


def cmd_verify(args) -> int:
    svc = _service(args)
    try:
        problems = svc.vault.verify()
        print(f"{sum(svc.vault.stats().values())} files checked, {len(problems)} problem(s)")
        return 1 if problems else 0
    finally:
        svc.lock()


def cmd_gui(args) -> int:
    from .gui.app import run

    return run(args.vault)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    from .crypto import CryptoError
    from .vault import VaultError

    if argv and argv[0] == "evaluate":
        from . import evaluate
        try:
            return evaluate.main(argv[1:])
        except (CryptoError, VaultError) as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1

    parser = argparse.ArgumentParser(prog="facedict", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--vault", type=Path, default=None, help="vault location (default: the app's vault)")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("gui", help="start the app (default)")
    q = sub.add_parser("query", help="search for a face")
    q.add_argument("photo")
    q.add_argument("--no-history", action="store_true", help="do not save this search")
    q.add_argument("--face", type=int, default=None, help="which face to show (1 = top-left); default: the largest")
    q.add_argument("--password-stdin", action="store_true")
    i = sub.add_parser("import", help="import <person>/ folders")
    i.add_argument("folder")
    i.add_argument("--password-stdin", action="store_true")
    v = sub.add_parser("verify", help="check vault integrity")
    v.add_argument("--password-stdin", action="store_true")
    sub.add_parser("evaluate", help="leave-one-out accuracy (see: evaluate --help)")
    args = parser.parse_args(argv)
    handlers = {"query": cmd_query, "import": cmd_import, "verify": cmd_verify}
    handler = handlers.get(args.command)
    if handler is None:
        return cmd_gui(args)
    try:
        return handler(args)
    except (CryptoError, VaultError, OSError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
