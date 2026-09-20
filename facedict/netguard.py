"""Two locks on the network.

1. `deny_network()` asks the macOS kernel to refuse every network system call of this process,
   so not even native code inside a dependency can open a connection.
2. `install()` replaces the Python socket calls, so a mistake in our own code fails loudly and
   the app is still protected where the kernel call is unavailable.

Local AF_UNIX sockets keep working, and so does every file operation.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import os
import socket

_IP_FAMILIES = {socket.AF_INET, socket.AF_INET6}
_originals: dict[tuple[object, str], object] = {}


class NetworkBlockedError(PermissionError):
    pass


def _deny(*_args, **_kwargs):
    raise NetworkBlockedError("Network access is disabled in FaceDictionary")


def _guard_method(name: str):
    original = getattr(socket.socket, name)

    def guarded(self, *args, **kwargs):
        if self.family in _IP_FAMILIES:
            _deny()
        return original(self, *args, **kwargs)

    guarded.__name__ = name
    return original, guarded


def install() -> None:
    if _originals:
        return
    for name in ("connect", "connect_ex", "bind", "sendto", "sendmsg"):
        original, guarded = _guard_method(name)
        _originals[(socket.socket, name)] = original
        setattr(socket.socket, name, guarded)
    for name in ("getaddrinfo", "gethostbyname", "gethostbyname_ex", "gethostbyaddr",
                 "create_connection", "create_server"):
        _originals[(socket, name)] = getattr(socket, name)
        setattr(socket, name, _deny)


def uninstall() -> None:
    """Restore the socket module (used by tests only)."""
    for (owner, name), original in _originals.items():
        setattr(owner, name, original)
    _originals.clear()


def installed() -> bool:
    return bool(_originals)


SANDBOX_PROFILE = b"(version 1)(allow default)(deny network*)"
_denied = False


def deny_network() -> bool:
    """Put this process into a macOS sandbox that allows everything except the network.
    It cannot be undone, which is the point. Returns True when the kernel accepted it."""
    global _denied
    if _denied or os.environ.get("FACEDICT_SANDBOXED") == "1":
        _denied = True
        return True
    if os.uname().sysname != "Darwin":
        return False
    try:
        lib = ctypes.CDLL(ctypes.util.find_library("System") or "/usr/lib/libSystem.dylib", use_errno=True)
        lib.sandbox_init.argtypes = [ctypes.c_char_p, ctypes.c_uint64, ctypes.POINTER(ctypes.c_char_p)]
        lib.sandbox_init.restype = ctypes.c_int
        error = ctypes.c_char_p()
        if lib.sandbox_init(SANDBOX_PROFILE, 0, ctypes.byref(error)) != 0:
            return False
    except (OSError, AttributeError):
        return False
    _denied = True
    os.environ["FACEDICT_SANDBOXED"] = "1"
    return True


def sandboxed() -> bool:
    """True when the kernel refuses this process's network calls."""
    return _denied or os.environ.get("FACEDICT_SANDBOXED") == "1"


def seal() -> str:
    """Lock this process out of the network as firmly as the system allows."""
    deny_network()
    install()
    return status()


def status() -> str:
    parts = []
    if sandboxed():
        parts.append("the macOS kernel (sandbox)")
    if installed():
        parts.append("an in-process guard")
    return " and ".join(parts) if parts else "nothing (no network guard is active)"
