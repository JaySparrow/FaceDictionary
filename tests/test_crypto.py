"""M1: key derivation, authenticated encryption, atomic writes."""

from __future__ import annotations

import os

import pytest

from facedict import crypto


def test_seal_roundtrip_and_aad_binding():
    key = crypto.random_key()
    blob = crypto.seal(key, b"secret", b"photo:1")
    assert b"secret" not in blob
    assert crypto.unseal(key, blob, b"photo:1") == b"secret"
    with pytest.raises(crypto.TamperedError):
        crypto.unseal(key, blob, b"photo:2")           # swapped identity
    with pytest.raises(crypto.TamperedError):
        crypto.unseal(crypto.random_key(), blob, b"photo:1")


@pytest.mark.parametrize("position", [0, 5, 20, -1])
def test_any_flipped_byte_is_detected(position):
    key = crypto.random_key()
    blob = bytearray(crypto.seal(key, b"x" * 100, b"a"))
    blob[position] ^= 0x01
    with pytest.raises(crypto.CryptoError):
        crypto.unseal(key, bytes(blob), b"a")


def test_nonces_are_unique():
    key = crypto.random_key()
    assert crypto.seal(key, b"same", b"a") != crypto.seal(key, b"same", b"a")


def test_password_key_is_deterministic_and_unicode_normalized():
    params = crypto.ScryptParams.generate(n=2 ** 12)
    composed, decomposed = "café", "café"
    assert crypto.derive_password_key(composed, params) == crypto.derive_password_key(decomposed, params)
    assert crypto.derive_password_key("a", params) != crypto.derive_password_key("b", params)
    assert crypto.ScryptParams.from_dict(params.to_dict()) == params


def test_recovery_code_format_and_normalization():
    code = crypto.new_recovery_code()
    groups = code.split("-")
    assert len(groups) == 8 and all(len(g) == 4 for g in groups)
    salt = os.urandom(16)
    k1 = crypto.derive_recovery_key(code, salt)
    k2 = crypto.derive_recovery_key(code.replace("-", " ").lower(), salt)
    assert k1 == k2


def test_atomic_write_replaces_and_sets_private_mode(tmp_path):
    target = tmp_path / "f.enc"
    crypto.atomic_write(target, b"one")
    crypto.atomic_write(target, b"two")
    assert target.read_bytes() == b"two"
    assert (target.stat().st_mode & 0o777) == 0o600
    assert [p.name for p in tmp_path.iterdir()] == ["f.enc"]


def test_atomic_write_failure_keeps_old_file(tmp_path, monkeypatch):
    target = tmp_path / "f.enc"
    crypto.atomic_write(target, b"old")

    def boom(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(crypto.os, "write", boom)
    with pytest.raises(OSError):
        crypto.atomic_write(target, b"new")
    monkeypatch.undo()
    assert target.read_bytes() == b"old"
    assert [p.name for p in tmp_path.iterdir()] == ["f.enc"]      # no temp files left behind
