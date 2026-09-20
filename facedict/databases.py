"""The list of databases (vaults) the user has opened, so the app can offer them at unlock.

Stored as plain JSON (paths only, never contents) in the app's state folder, mode 600.
The user can forget single entries or clear the whole list from the unlock screen.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from . import config

MAX_REMEMBERED = 8
FILENAME = "databases.json"


def store_path() -> Path:
    return config.state_dir() / FILENAME


@dataclass
class DatabaseRef:
    path: Path
    last_opened: str = ""

    @property
    def name(self) -> str:
        return display_name(self.path)

    def to_dict(self) -> dict:
        return {"path": str(self.path), "last_opened": self.last_opened}


def display_name(path: Path) -> str:
    path = Path(path)
    if path == config.DEFAULT_VAULT_DIR:
        return "My database"
    name = path.name
    for suffix in (".noindex", ".facedict"):
        if name.endswith(suffix):
            name = name[: -len(suffix)]
    return name or str(path)


def _read() -> list[DatabaseRef]:
    try:
        raw = json.loads(store_path().read_text("utf-8"))
    except (OSError, ValueError):
        return []
    refs = []
    for item in raw.get("databases", []):
        try:
            refs.append(DatabaseRef(Path(item["path"]), str(item.get("last_opened", ""))))
        except (KeyError, TypeError):
            continue
    return refs


def _write(refs: list[DatabaseRef]) -> None:
    path = store_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    payload = json.dumps({"databases": [r.to_dict() for r in refs[:MAX_REMEMBERED]]}, ensure_ascii=False, indent=1)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(payload)


def recent() -> list[DatabaseRef]:
    """Databases that still exist on disk, most recently opened first."""
    return [r for r in _read() if (r.path / "vault.json").is_file()]


def remember(path: Path) -> None:
    path = Path(path).expanduser().resolve()
    refs = [r for r in _read() if r.path != path]
    refs.insert(0, DatabaseRef(path, datetime.now().astimezone().isoformat(timespec="seconds")))
    _write(refs)


def forget(path: Path) -> None:
    path = Path(path).expanduser().resolve()
    _write([r for r in _read() if r.path != path])


def clear() -> None:
    store_path().unlink(missing_ok=True)


def last_used() -> Path | None:
    refs = recent()
    return refs[0].path if refs else None


def startup_vault() -> Path:
    """Which database to offer first: the one named by FACEDICT_VAULT, else the last one used,
    else the default location."""
    override = os.environ.get("FACEDICT_VAULT")
    if override:
        return Path(override).expanduser().resolve()
    return last_used() or config.DEFAULT_VAULT_DIR
