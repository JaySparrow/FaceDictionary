# FaceDictionary

A private face lookup for your own dictionary of people, for macOS.

Give it a photo. It finds every face in it, compares each one with the faces in your library and
shows the five entries that look most alike, with a similarity score. Confirm a match and the photo
joins that person's album.

Everything happens on your Mac: entries, photos, search history and face data live in an encrypted
database, and the app runs with its network access switched off at the kernel level.

## Install

Download or build `FaceDictionary.dmg`, open it and drag the app into *Applications*. It carries
everything it needs — Python, Qt, OpenCV and the face models — so nothing else has to be installed.

On first start you choose a password for a new database and then three security questions that can
recover it. Your database is kept in `~/Library/Application Support/FaceDictionary/` and survives
app updates.

> An app built on your own Mac opens with a double-click. One downloaded from elsewhere is
> quarantined by macOS: allow it once under *System Settings → Privacy & Security → Open Anyway*.

## Using it

| Page | What you can do |
|---|---|
| **Search** | Drop a photo on the frame, click the empty frame or **+** to choose a file, ⌘V to paste, ⌘O to open. Every face is searched; the largest is shown first. Click any face box (or its thumbnail, or ←/→, 1–9) to see that face's top five. Clicking a result's photo or name opens that entry; **✓ This is the person** adds the photo to its album, using that face. In a group photo you can confirm several faces for several people. |
| **History** | Every past search with the stored result of every face, exactly as it was — nothing is recomputed. Confirm or undo from here, or search the same photo again with the library as it is now. |
| **Library** | Text reads like a label: **double-click** a name, alias, tag or field to change it (a click is enough where nothing is written yet), then press Enter or click elsewhere to save, or Esc to keep the old text; fields of several lines save with ⌘Enter. Right-click beside the fields (or use *Edit*) to add one. The three buttons above the album add photos (so do ⌘O and dropping photos on it), move the selected one to the Trash, or stop using it for matching; photos can be dragged onto another photo to swap them, or between two to insert. The first photo, framed, is the entry's cover. Double-click a photo to see it big, with a note of its own under it; in a photo with several faces, double-click the face to use for matching. |
| **Trash** | Deleted entries, photos and searches wait here until you remove them for good (it asks twice). Nothing is ever deleted automatically. |

Shortcuts: ⌘1–⌘4 switch pages · ⌘O open a photo · ⌘V paste · ⌘F find an entry · ⌘N new entry · ⌘L lock ·
⌘Z / ⇧⌘Z undo and redo any change in the Library, step after step (also in *Edit*) ·
⌫ deletes what is selected in the list or album you are in, as its delete button does ·
⌘-click and ⇧-click select several entries, photos, searches or Trash items; ⌫, the right-click menu,
the album's buttons and ⌘C then act on all of them at once (in the Library, one ⌘Z undoes it).

**Fields.** A *shared* field exists on every entry of the database, each with its own text (an empty
one is shown dimmed, ready to fill in); a field *only in this entry* stays where you made it, below a
thin line. *Add Field to All Entries* (⇧⌘;) and *Add Field to This Entry* (⌘;) are in the right-click
menu and in *Edit*: type the name, press Enter and go on with its text. Double-click a field's name to
rename it; clear its name, or right-click it and choose *Remove*, to remove it.
Removing a shared field takes it off every entry, but text that was written in it is kept as a
field of that entry, so nothing is lost — and ⌘Z puts it back.

**Undo.** Every change in the Library — text, fields, photos added, removed, reordered or switched
off, the face used in a photo, entries created or deleted — can be undone with ⌘Z, as many steps back
as you like, and redone with ⇧⌘Z. Undoing an addition moves it to the Trash rather than deleting
it. The history lives in memory only and is forgotten when the app locks.

**Finding an entry.** The box above the list searches names, aliases, tags and every shared field at
once; the drop-down next to it narrows the search to one of them.

**Several databases.** Each database is a self-contained encrypted folder with its own password and
its own recovery questions. *Open another…* on the unlock screen lists the ones you have opened,
picks a different folder, or creates a new one. The app checks a folder before doing anything: it
says whether it is a database, whether the format is supported and how its password can be recovered.

**Importing.** *Import…* reads a folder that holds one sub-folder per person, each with that person's
photos and optionally an `entry.json` (`name`, `aliases`, `tags`, `notes`, anything else). Your
original files are only read — never changed, moved or deleted.

**Photos.** JPEG, PNG, HEIC/HEIF, WebP, BMP and TIFF, rotated as their EXIF says. Names, tags and
fields can be written in any script; searching ignores case and full-width/half-width forms, and
commas, `，`, `、` and `;` all separate aliases and tags.

## Privacy

| Concern | What the app does |
|---|---|
| Data leaving the Mac | At start-up the app asks the macOS kernel to refuse every network call it makes, and replaces Python's socket functions as a second layer. It contains no network code. Only `scripts/fetch_models.py` downloads anything, and only the models. |
| Data at rest | AES-256-GCM per file, under a random data key that your password unlocks (scrypt). File names are random ids; the folder is not synced by iCloud and not indexed by Spotlight. |
| Tampering | Every file is authenticated, so a changed or swapped one is detected. *Settings → Check database integrity* verifies them all. |
| Unattended app | Locks after five idle minutes or with ⌘L: keys, index, images and the undo history leave memory, open dialogs close and text being typed is saved first. Locking never touches the files. |
| Traces | No logs. Decrypted photos never reach the disk. The app's settings store only the window size, and Qt's file-dialog history is erased after every use; the folder a photo dialog last opened is remembered only until the app quits. The paths (not the contents) of databases you opened are remembered so they can be offered at unlock; that list can be cleared from the unlock screen. |
| Losing data | Deleting moves things to the Trash; all writes are atomic; rebuilding the index only rewrites caches; one window at a time may open a database. |
| Forgotten password | Answer the security questions (any two of three by default). A printed recovery code is available as a second method in *Settings*. Anyone who can answer the questions can open that database, so choose answers only you know — they need not be true. |

Recommended as well: keep FileVault on, keep Time Machine backups (they hold only ciphertext), and
turn off *System Settings → Privacy & Security → Analytics & Improvements → Share Mac Analytics*.

## Recognition

Detection is OpenCV's YuNet, at two resolutions so that small faces in group photos are found too.
Recognition can use any of these; switch in *Settings*:

| Model | Size | Full LFW (5,749 people): top-1 / top-5 | Time per face |
|---|---|---|---|
| **ArcFace MobileFaceNet** (default) | 14 MB | 98.9% / 99.3% | 6 ms |
| SFace | 39 MB | 98.4% / 99.1% | 6 ms |
| ArcFace ResNet-50 (optional) | 174 MB | 98.9% / 99.4% | 29 ms |

To see how they do on **your** library, run this in your own terminal; it only prints totals and
changes nothing:

```bash
python -m facedict evaluate
```

## From source

```bash
conda env create -f environment.yml
conda activate facedict
python scripts/fetch_models.py      # ~53 MB, verified by SHA-256
./run.command                       # start the app
python -m pytest                    # ~170 tests; temp databases only, GUI runs offscreen
```

Command line: `python -m facedict query PHOTO [--face N]`, `import FOLDER`, `evaluate`, `verify`;
`--vault PATH` picks another database. The packaged app understands the same commands.

`scripts/prepare_testdata.py` and `scripts/build_demo.py` fetch the public LFW faces and build a
demo database of 150 people (password `demo-password`) that `./run_demo.command` opens, with query
photos that are deliberately *not* in it.

Building the app and the installer: see [docs/packaging.md](docs/packaging.md).
The design decisions and their history are in [docs/design-notes.md](docs/design-notes.md), plans for larger
changes in [docs/plans/](docs/plans/), and the operating hints that were taken out of the interface in
[docs/in-app-hints.md](docs/in-app-hints.md) (all in Chinese).

## Layout

```
facedict/
  netguard.py   kernel + in-process network lock     crypto.py   scrypt, AES-GCM, atomic writes
  vault.py      encrypted store, history, trash      models.py   data records
  databases.py  the list of databases                imageio.py  in-memory decoding (HEIC, EXIF)
  engine.py     YuNet + alignment + ArcFace/SFace    index.py    embeddings, top-k per entry
  service.py    application logic                    evaluate.py leave-one-out accuracy
  gui/          the PySide6 interface; undo.py holds the undo history of the Library
scripts/        fetch_models · prepare_testdata · build_demo · build_app
packaging/      PyInstaller spec, icon, dmg settings
tests/          unit, integration and GUI tests
```

## Licence

FaceDictionary is released under the **PolyForm Noncommercial License 1.0.0** — free for personal,
research and other noncommercial use; commercial use is not granted. See `LICENSE`, and
[THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md) for the libraries and face models it builds on
(the InsightFace weights are themselves noncommercial-only).
