"""Key derivation, authenticated encryption and crash-safe file writes.

Blob format:  b"FDV" | version (1 byte) | nonce (12 bytes) | AES-256-GCM ciphertext+tag
Every blob is bound to its identity through the associated data (e.g. b"photo:<id>"),
so a file swapped for another one fails authentication.
"""

from __future__ import annotations

import base64
import fcntl
import hashlib
import os
import re
import secrets
import unicodedata
import uuid
from dataclasses import dataclass
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

MAGIC = b"FDV"
VERSION = 1
NONCE_LEN = 12
KEY_LEN = 32
RECOVERY_BYTES = 20                     # 160-bit recovery code -> 32 base32 characters


class CryptoError(Exception):
    pass


class WrongPasswordError(CryptoError):
    pass


class TamperedError(CryptoError):
    pass


def random_key() -> bytes:
    return secrets.token_bytes(KEY_LEN)


def b64e(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def b64d(text: str) -> bytes:
    return base64.b64decode(text.encode("ascii"), validate=True)


# --- key derivation -----------------------------------------------------------

@dataclass(frozen=True)
class ScryptParams:
    salt: bytes
    n: int = 2 ** 17
    r: int = 8
    p: int = 1

    def to_dict(self) -> dict:
        return {"name": "scrypt", "n": self.n, "r": self.r, "p": self.p, "salt": b64e(self.salt)}

    @classmethod
    def from_dict(cls, d: dict) -> "ScryptParams":
        if d.get("name") != "scrypt":
            raise CryptoError(f"Unsupported KDF {d.get('name')!r}")
        return cls(salt=b64d(d["salt"]), n=int(d["n"]), r=int(d["r"]), p=int(d["p"]))

    @classmethod
    def generate(cls, n: int = 2 ** 17) -> "ScryptParams":
        return cls(salt=secrets.token_bytes(16), n=n)


def derive_password_key(password: str, params: ScryptParams) -> bytes:
    normalized = unicodedata.normalize("NFC", password).encode("utf-8")
    maxmem = 128 * params.r * params.n * 2 + (1 << 20)
    return hashlib.scrypt(normalized, salt=params.salt, n=params.n, r=params.r, p=params.p,
                          maxmem=maxmem, dklen=KEY_LEN)


def new_recovery_code() -> str:
    raw = base64.b32encode(secrets.token_bytes(RECOVERY_BYTES)).decode("ascii")
    return "-".join(raw[i:i + 4] for i in range(0, len(raw), 4))


def normalize_recovery_code(code: str) -> str:
    return "".join(ch for ch in code.upper() if ch.isalnum())


def derive_recovery_key(code: str, salt: bytes) -> bytes:
    raw = normalize_recovery_code(code).encode("ascii")
    return HKDF(algorithm=hashes.SHA256(), length=KEY_LEN, salt=salt,
                info=b"facedict-recovery-v1").derive(raw)


# --- security question answers ------------------------------------------------------------

def normalize_answer(answer: str) -> str:
    """Answers are compared forgivingly: case, spaces, punctuation and accents don't matter."""
    text = unicodedata.normalize("NFKD", answer).casefold()
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return re.sub(r"[^0-9a-z\u0080-\uffff]+", "", unicodedata.normalize("NFKC", text))


def combine_answers(answers: list[str]) -> bytes:
    normalized = [normalize_answer(a) for a in answers]
    if any(len(a) < 1 for a in normalized):
        raise ValueError("Every answer must contain at least one letter or digit")
    return "\x1f".join(normalized).encode("utf-8")


def derive_answers_key(answers: list[str], params: ScryptParams, indexes: tuple[int, ...]) -> bytes:
    """Key for one combination of answers; the question indexes are mixed into the salt."""
    salt = params.salt + b"|" + ",".join(str(i) for i in indexes).encode("ascii")
    maxmem = 128 * params.r * params.n * 2 + (1 << 20)
    return hashlib.scrypt(combine_answers(answers), salt=salt, n=params.n, r=params.r, p=params.p,
                          maxmem=maxmem, dklen=KEY_LEN)


# --- authenticated encryption -------------------------------------------------

def seal(key: bytes, plaintext: bytes, aad: bytes) -> bytes:
    nonce = secrets.token_bytes(NONCE_LEN)
    return MAGIC + bytes([VERSION]) + nonce + AESGCM(key).encrypt(nonce, plaintext, aad)


def unseal(key: bytes, blob: bytes, aad: bytes) -> bytes:
    header = len(MAGIC) + 1
    if len(blob) < header + NONCE_LEN + 16 or blob[:len(MAGIC)] != MAGIC:
        raise TamperedError("Not a FaceDictionary encrypted file")
    if blob[len(MAGIC)] != VERSION:
        raise CryptoError(f"Unsupported file version {blob[len(MAGIC)]}")
    nonce = blob[header:header + NONCE_LEN]
    try:
        return AESGCM(key).decrypt(nonce, blob[header + NONCE_LEN:], aad)
    except InvalidTag:
        raise TamperedError("Decryption failed: wrong key or the file was modified") from None


# --- crash-safe writes --------------------------------------------------------

def atomic_write(path: Path, data: bytes, durable: bool = True) -> None:
    """Write to a temp file in the same directory, flush to disk, then rename over `path`.

    A crash at any point leaves either the old file or the new file, never a partial one.
    """
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            view = view[written:]
        _flush(fd, durable)
    except BaseException:
        os.close(fd)
        tmp.unlink(missing_ok=True)
        raise
    os.close(fd)
    os.replace(tmp, path)
    if durable:
        dir_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)


def _flush(fd: int, durable: bool) -> None:
    if durable and hasattr(fcntl, "F_FULLFSYNC"):
        try:
            fcntl.fcntl(fd, fcntl.F_FULLFSYNC)     # macOS: flush the drive's cache too
            return
        except OSError:
            pass
    os.fsync(fd)
