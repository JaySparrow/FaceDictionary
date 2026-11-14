# PyInstaller build of FaceDictionary.app  —  run it through scripts/build_app.py
import os
from pathlib import Path

PROJECT = Path(SPECPATH).resolve().parent
VERSION = os.environ.get("FACEDICT_VERSION", "1.0.0")
MODELS = os.environ.get("FACEDICT_BUNDLE_MODELS", "").split(os.pathsep)

datas = [(str(PROJECT / "models" / name), "models") for name in MODELS if name]
datas += [(str(PROJECT / name), ".") for name in ("LICENSE", "THIRD-PARTY-NOTICES.md")]     # Contents/Resources

# Qt modules the app never uses; leaving them out keeps the bundle smaller
excludes = [
    "PySide6.QtQml", "PySide6.QtQuick", "PySide6.QtQuickWidgets", "PySide6.Qt3DCore", "PySide6.QtCharts",
    "PySide6.QtDataVisualization", "PySide6.QtMultimedia", "PySide6.QtMultimediaWidgets", "PySide6.QtWebSockets",
    "PySide6.QtWebChannel", "PySide6.QtSql", "PySide6.QtTest", "PySide6.QtHelp", "PySide6.QtDesigner",
    "PySide6.QtOpenGL", "PySide6.QtOpenGLWidgets", "PySide6.QtPdf", "PySide6.QtPdfWidgets", "PySide6.QtSpatialAudio",
    "PySide6.QtSerialPort", "PySide6.QtBluetooth", "PySide6.QtNfc", "PySide6.QtPositioning", "PySide6.QtSensors",
    "PySide6.QtRemoteObjects", "PySide6.QtScxml", "PySide6.QtStateMachine", "PySide6.QtTextToSpeech",
    "tkinter", "matplotlib", "pytest", "IPython", "scipy", "pandas", "setuptools", "pip",
]

a = Analysis(
    [str(PROJECT / "packaging" / "launcher.py")],
    pathex=[str(PROJECT)],
    binaries=[],
    datas=datas,
    hiddenimports=["facedict.gui.app"],
    excludes=excludes,
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="FaceDictionary",
    console=False,                       # no terminal window
    debug=False,
    strip=False,
    upx=False,
    target_arch=None,                    # the architecture this Mac runs
    codesign_identity=None,              # ad-hoc signature (required on Apple silicon)
    entitlements_file=None,
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="FaceDictionary")

app = BUNDLE(
    coll,
    name="FaceDictionary.app",
    icon=str(PROJECT / "packaging" / "FaceDictionary.icns"),
    bundle_identifier="local.facedictionary.app",
    version=VERSION,
    info_plist={
        "CFBundleName": "FaceDictionary",
        "CFBundleDisplayName": "FaceDictionary",
        "CFBundleShortVersionString": VERSION,
        "CFBundleVersion": VERSION,
        "LSMinimumSystemVersion": "13.0",
        "LSApplicationCategoryType": "public.app-category.utilities",
        "NSHighResolutionCapable": True,
        "NSRequiresAquaSystemAppearance": False,     # follow light / dark mode
        "NSHumanReadableCopyright": "Personal, non-commercial use.",
    },
)
