"""Several databases: the remembered list, the startup choice and switching between them."""

from __future__ import annotations

import pytest

from facedict import config, databases
from facedict.service import FaceDictionaryService, ServiceError
from facedict.vault import Vault

PW = "databases-test-password"


def make_vault(path) -> None:
    Vault.create(path, PW, kdf_n=2 ** 12)


def test_remember_forget_and_clear(tmp_path):
    a, b = tmp_path / "A.noindex", tmp_path / "B.noindex"
    make_vault(a)
    make_vault(b)
    assert databases.recent() == []
    databases.remember(a)
    databases.remember(b)
    assert [r.path for r in databases.recent()] == [b, a]        # most recent first
    assert [r.name for r in databases.recent()] == ["B", "A"]
    databases.remember(a)
    assert [r.path for r in databases.recent()] == [a, b]
    assert (databases.store_path().stat().st_mode & 0o777) == 0o600
    databases.forget(b)
    assert [r.path for r in databases.recent()] == [a]
    databases.clear()
    assert databases.recent() == [] and not databases.store_path().exists()


def test_recent_hides_databases_that_are_gone(tmp_path):
    a = tmp_path / "Gone.noindex"
    make_vault(a)
    databases.remember(a)
    assert len(databases.recent()) == 1
    (a / "vault.json").unlink()
    assert databases.recent() == []


def test_startup_vault_prefers_env_then_last_used(tmp_path, monkeypatch):
    a = tmp_path / "Last.noindex"
    make_vault(a)
    assert databases.startup_vault() == tmp_path / "vault.noindex"       # FACEDICT_VAULT from the fixture
    monkeypatch.delenv("FACEDICT_VAULT")
    assert databases.startup_vault() == config.DEFAULT_VAULT_DIR
    databases.remember(a)
    assert databases.startup_vault() == a
    assert FaceDictionaryService().vault_root == a                        # a new app run opens it again


def test_each_database_has_its_own_password(tmp_path):
    a, b = tmp_path / "Family.noindex", tmp_path / "Work.noindex"
    svc = FaceDictionaryService(a)
    svc.create_vault("password for A", kdf_n=2 ** 12)
    svc.create_entry("Someone in A")
    svc.lock()
    svc.use_database(b)
    assert svc.vault_name == "Work" and svc.inspect().can_create
    svc.create_vault("password for B", kdf_n=2 ** 12)
    assert [e.name for e in svc.list_entries()] == []
    svc.lock()
    svc.use_database(a)
    from facedict.crypto import WrongPasswordError
    with pytest.raises(WrongPasswordError):
        svc.unlock("password for B")
    svc.unlock("password for A")
    assert [e.name for e in svc.list_entries()] == ["Someone in A"]
    assert [r.name for r in svc.recent_databases()] == ["Family", "Work"]


def test_switching_requires_locking_first(tmp_path):
    a, b = tmp_path / "One.noindex", tmp_path / "Two.noindex"
    svc = FaceDictionaryService(a)
    svc.create_vault(PW, kdf_n=2 ** 12)
    with pytest.raises(ServiceError):
        svc.use_database(b)
    svc.lock()
    info = svc.use_database(b)
    assert info.can_create and svc.vault_root == b.resolve()


def test_inspect_reports_a_folder_that_is_not_a_database(tmp_path):
    other = tmp_path / "photos"
    other.mkdir()
    (other / "a.jpg").write_bytes(b"x")
    svc = FaceDictionaryService(tmp_path / "x.noindex")
    svc.lock()
    info = svc.use_database(other)
    assert info.error and not info.usable and not info.can_create
