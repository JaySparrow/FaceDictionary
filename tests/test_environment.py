"""M0: environment, model integrity and the network-denying launcher."""

from __future__ import annotations

import subprocess
import sys

import pytest

from pathlib import Path

from facedict import config
from tests.conftest import ROOT


def test_runtime_imports():
    import cryptography  # noqa: F401
    import cv2
    import numpy  # noqa: F401
    import PIL  # noqa: F401
    import pillow_heif  # noqa: F401
    import PySide6  # noqa: F401

    assert cv2.__version__.startswith("5.")
    assert hasattr(cv2, "FaceDetectorYN")


def test_models_present_and_verified(models_available):
    result = subprocess.run([sys.executable, str(ROOT / "scripts" / "fetch_models.py"), "--verify"],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr


def test_default_vault_outside_project():
    assert config.PROJECT_ROOT not in config.DEFAULT_VAULT_DIR.parents
    assert config.DEFAULT_VAULT_DIR.name.endswith(".noindex")


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS sandbox only")
def test_the_kernel_refuses_network_calls_after_deny_network():
    """netguard.deny_network() seals the process at the kernel level - test it in a child process,
    because it cannot be undone."""
    code = (
        "import socket, sys\n"
        "sys.path.insert(0, %r)\n"
        "from facedict import netguard\n"
        "assert netguard.deny_network() is True\n"
        "assert netguard.sandboxed() and not netguard.installed()\n"   # kernel only, no Python guard yet
        "try:\n"
        "    socket.create_connection(('1.1.1.1', 443), timeout=3); print('REACHABLE')\n"
        "except OSError as exc:\n"
        "    print('BLOCKED', type(exc).__name__)\n"
        "open('/etc/hosts').close(); print('FILES OK')\n"
    ) % str(ROOT)
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    assert "BLOCKED" in out.stdout and "FILES OK" in out.stdout


def test_launcher_starts_the_app_without_a_terminal_window():
    launcher = (ROOT / "run.command").read_text()
    assert "-m facedict" in launcher and "sandbox-exec" not in launcher   # the app sandboxes itself now


# --- packaging ----------------------------------------------------------------------------------

def test_bundled_paths_point_into_the_app(monkeypatch, tmp_path):
    """Inside FaceDictionary.app the models sit next to the code, not in the source tree."""
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    assert config.bundled() and config.resource_dir() == tmp_path
    assert config.models_dir() == tmp_path / "models"
    monkeypatch.delenv("FACEDICT_MODELS", raising=False)
    monkeypatch.delattr(sys, "frozen")
    assert not config.bundled() and config.models_dir() == config.PROJECT_ROOT / "models"
    # the database never moves into the bundle
    assert config.DEFAULT_VAULT_DIR.is_relative_to(Path.home())


def test_packaging_files_are_present_and_consistent():
    import facedict
    spec = (ROOT / "packaging" / "FaceDictionary.spec").read_text()
    assert "BUNDLE(" in spec and "FaceDictionary.icns" in spec and "console=False" in spec
    for name in ("build_app.py", "fetch_models.py"):
        assert (ROOT / "scripts" / name).exists()
    assert (ROOT / "packaging" / "dmg_settings.py").exists()
    assert (ROOT / "environment-build.yml").exists()
    assert facedict.__version__ in (ROOT / "pyproject.toml").read_text()


def test_only_open_licence_models_can_be_bundled_for_sharing(monkeypatch):
    sys.path.insert(0, str(ROOT / "scripts"))
    import build_app

    monkeypatch.setattr(build_app, "ROOT", ROOT)
    shared = build_app.pick_models(open_only=True, with_optional=False)
    assert all("w600k" not in name for name in shared)          # no InsightFace weights
    assert any("yunet" in name for name in shared)
