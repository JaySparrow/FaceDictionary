# Building the app and the installer

The build turns the source tree into `FaceDictionary.app` — a self-contained macOS application that
carries Python, Qt, OpenCV and the face models — and wraps it in a `.dmg` for installing.

## Once: the build environment

The packaging environment is separate from the development one and installs everything from pip
wheels, because those carry their own native libraries (this matters for `pillow-heif`).

```bash
conda env create -f environment-build.yml
conda activate facedict-build
python scripts/fetch_models.py        # the models that go into the bundle
```

## Every build

```bash
conda activate facedict-build
python scripts/build_app.py                 # -> dist/FaceDictionary.app and dist/FaceDictionary-<version>.dmg
python scripts/build_app.py --app-only      # skip the disk image
python scripts/build_app.py --open-models   # bundle only the Apache-2.0 models (see below)
python scripts/build_app.py --with-r50      # also the optional 174 MB ResNet-50 model
```

The script draws the icon, runs PyInstaller with `packaging/FaceDictionary.spec`, signs the bundle
ad-hoc (Apple silicon refuses unsigned binaries), checks that the bundled binary starts, and builds
the disk image with `dmgbuild`.

Typical result: **≈290 MB app, ≈150 MB dmg**, three to eight minutes.

## What the bundle contains

* `Contents/MacOS/FaceDictionary` — the app; it also accepts the command line (`query`, `import`,
  `evaluate`, `verify`), which is how the build checks itself.
* `Contents/Frameworks/` — Python, Qt (only the modules the app uses), OpenCV, NumPy, Pillow,
  pillow-heif, cryptography.
* `models/` — YuNet, ArcFace MobileFaceNet and SFace; their SHA-256 is still checked at run time.

Databases are **not** part of the bundle: they stay in `~/Library/Application Support/FaceDictionary/`
(or wherever you put them), so replacing the app never touches your data.

## Sharing the installer

Model weights from InsightFace are licensed for non-commercial use only. If you pass the `.dmg` to
other people, build it with `--open-models`: the bundle then carries only the Apache-2.0 models
(YuNet detection, SFace recognition), which costs about half a percent of top-1 accuracy.

A `.dmg` built on your own Mac is not quarantined, so the app opens with a double-click. One that
someone downloads is quarantined by macOS; because the app is signed ad-hoc rather than notarised,
they have to allow it once under *System Settings → Privacy & Security → Open Anyway*. Notarising it
would need a paid Apple Developer account.

Qt is used under the LGPL: anyone who receives the app may relink it against their own Qt build by
recreating the environment above and re-running this script.

## How the app blocks the network

At start-up `facedict/netguard.py` calls the macOS `sandbox_init` with a profile that allows
everything except the network, so the kernel refuses network system calls for the process — native
code included. Python's own socket functions are replaced as well, so a mistake in the app's code
fails loudly. *Settings* shows which of the two layers are active.
