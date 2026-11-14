"""Undo and redo for every change made in the Library.

Works like the Edit menu of any Mac app: one history, ⌘Z steps back through it as far as you like,
⇧⌘Z steps forward again, and a new change after an undo drops the steps that were undone.

Pushing a command makes the change (QUndoStack.push() runs redo()), except for changes that a
background task has made already (`done_already`). Later undo()/redo() calls replay it through the
service. If the world changed in the meantime (say, the Trash was emptied), the step says why it
cannot be replayed and drops out of the history instead of half-applying.
"""

from __future__ import annotations

from typing import Callable

from PySide6.QtGui import QUndoCommand

from ..models import Face, FieldDef, OwnField, new_id
from ..service import RemovedField

Done = Callable[[str | None], None]                 # show this entry after a step (None: no entry)
Failed = Callable[["LibraryCommand", Exception], None]


def merged_order(current: list[str], wanted: list[str]) -> list[str]:
    """`wanted` order for the photos that are still there; photos it does not know go last."""
    present = set(current)
    head = [pid for pid in wanted if pid in present]
    known = set(head)
    return head + [pid for pid in current if pid not in known]


def plural(n: int, word: str) -> str:
    return f"{n} {word}s" if n != 1 else word


class LibraryCommand(QUndoCommand):
    def __init__(self, service, entry_id: str | None, text: str, done: Done, failed: Failed,
                 done_already: bool = False):
        super().__init__(text)
        self.service = service
        self.entry_id = entry_id
        self.phase = "do"                            # "do" (the push), "undo" or "redo"
        self._done = done
        self._failed = failed
        self._skip_first = done_already
        self._pushed = False

    # QUndoStack calls these
    def redo(self) -> None:
        if not self._pushed:
            self._pushed = True
            if self._skip_first:
                return
        else:
            self.phase = "redo"
        self._step(self.apply)

    def undo(self) -> None:
        self.phase = "undo"
        self._step(self.revert)

    def _step(self, fn) -> None:
        try:
            fn()
        except Exception as exc:                     # the world changed: drop this step, say why
            self.setObsolete(True)
            self._failed(self, exc)
            return
        self._done(self.shown_entry())

    def shown_entry(self) -> str | None:
        """The entry to show after a step."""
        if self.entry_id and self.service.has_entry(self.entry_id):
            return self.entry_id
        return None

    # subclasses
    def apply(self) -> None:
        raise NotImplementedError

    def revert(self) -> None:
        raise NotImplementedError

    def _entry(self):
        return self.service.get_entry(self.entry_id)


# --- text ---------------------------------------------------------------------------------------

class SetText(LibraryCommand):
    """The name, the aliases, the tags, or the text of one field. `key` is "name", "aliases", "tags",
    ("field", field id) or ("own", field name)."""

    def __init__(self, service, entry_id, key, old, new, label: str, **kw):
        super().__init__(service, entry_id, f"Edit {label}", **kw)
        self.key, self.old, self.new = key, old, new

    def apply(self) -> None:
        self._set(self.new)

    def revert(self) -> None:
        self._set(self.old)

    def _set(self, value) -> None:
        e = self._entry()
        if self.key == "name":
            e.name = value
        elif self.key == "aliases":
            e.aliases = list(value)
        elif self.key == "tags":
            e.tags = list(value)
        elif self.key[0] == "field":
            e.fields[self.key[1]] = value
        else:
            own = e.own_field(self.key[1])
            if own is None:
                e.own.append(OwnField(self.key[1], value))
            else:
                own.value = value
        self.service.update_entry(e)


# --- fields ---------------------------------------------------------------------------------------

class OwnFieldChange(LibraryCommand):
    """A field of this entry only, added (`added`=True) or removed, at `index` among its own fields."""

    def __init__(self, service, entry_id, field: OwnField, index: int, added: bool, **kw):
        verb = "Add" if added else "Remove"
        super().__init__(service, entry_id, f"{verb} Field “{field.name}”", **kw)
        self.field = OwnField(field.name, field.value)
        self.index, self.added = index, added

    def apply(self) -> None:
        (self._insert if self.added else self._remove)()

    def revert(self) -> None:
        (self._remove if self.added else self._insert)()

    def _insert(self) -> None:
        e = self._entry()
        if e.own_field(self.field.name) is None:
            e.own.insert(min(self.index, len(e.own)), OwnField(self.field.name, self.field.value))
            self.service.update_entry(e)

    def _remove(self) -> None:
        e = self._entry()
        own = e.own_field(self.field.name)
        if own is not None:
            self.field.value = own.value                # what it says now comes back on undo
            e.own.remove(own)
            self.service.update_entry(e)


class RenameField(LibraryCommand):
    """`field_id` set: a shared field; otherwise a field of this entry only."""

    def __init__(self, service, entry_id, field_id: str | None, old: str, new: str, **kw):
        super().__init__(service, entry_id, "Rename Field", **kw)
        self.field_id, self.old, self.new = field_id, old, new

    def apply(self) -> None:
        self._rename(self.old, self.new)

    def revert(self) -> None:
        self._rename(self.new, self.old)

    def _rename(self, before: str, after: str) -> None:
        if self.field_id:
            self.service.rename_global_field(self.field_id, after)
            return
        e = self._entry()
        own = e.own_field(before)
        if own is None:
            raise LookupError(f"the field “{before}” is no longer there")
        own.name = after
        self.service.update_entry(e)


class SharedFieldChange(LibraryCommand):
    """A shared field added (`added`=True) or removed. Removing keeps every entry's text as a field of
    that entry; undoing the removal puts the field back with each entry's text in it."""

    def __init__(self, service, entry_id, removed: RemovedField, added: bool, **kw):
        verb = "Add" if added else "Remove"
        super().__init__(service, entry_id, f"{verb} Shared Field “{removed.spec.name}”", **kw)
        self.removed, self.added = removed, added

    @classmethod
    def new(cls, service, entry_id, name: str, **kw) -> "SharedFieldChange":
        spec = FieldDef(new_id(), name.strip())
        return cls(service, entry_id, RemovedField(spec, len(service.global_fields())), True, **kw)

    @property
    def field_id(self) -> str:
        return self.removed.spec.id

    def apply(self) -> None:
        (self._reinstate if self.added else self._drop)()

    def revert(self) -> None:
        (self._drop if self.added else self._reinstate)()

    def _drop(self) -> None:
        removed = self.service.drop_global_field(self.removed.spec.id, keep_content=True)
        if removed is None:
            raise LookupError(f"the field “{self.removed.spec.name}” is no longer there")
        self.removed = removed

    def _reinstate(self) -> None:
        self.service.reinstate_global_field(self.removed)


# --- photos ----------------------------------------------------------------------------------------

class _PhotoCommand(LibraryCommand):
    def _ids(self) -> list[str]:
        return [p.photo_id for p in self._entry().photos]

    def _order(self, wanted: list[str]) -> None:
        current = self._ids()
        order = merged_order(current, wanted)
        if order != current:
            self.service.set_photo_order(self.entry_id, order)

    def _take(self, photo_id: str) -> str | None:
        """Move a photo of this entry to the Trash; returns the trash item (None: it was gone already)."""
        if self._entry().photo(photo_id) is None:
            return None
        return self.service.remove_photo(self.entry_id, photo_id).id

    def _give_back(self, photo_id: str, trash_id: str | None) -> None:
        if self._entry().photo(photo_id) is not None:          # put back from the Trash page already
            return
        if trash_id is None or not self.service.in_trash(trash_id):
            raise LookupError("the photo is no longer in the Trash")
        self.service.restore(trash_id)


class AddPhotos(_PhotoCommand):
    """Photos added to an album (by a background task). Undo moves them to the Trash; redo brings
    them back from there."""

    def __init__(self, service, entry_id, photo_ids: list[str], **kw):
        super().__init__(service, entry_id, f"Add {plural(len(photo_ids), 'Photo')}", done_already=True, **kw)
        self.photo_ids = list(photo_ids)
        self.trash: dict[str, str | None] = {}
        self.order: list[str] = []                     # the album with these photos in it

    def revert(self) -> None:
        self.order = self._ids()
        self.trash = {pid: self._take(pid) for pid in self.photo_ids}

    def apply(self) -> None:
        for pid in self.photo_ids:
            self._give_back(pid, self.trash.get(pid))
        self._order(self.order)


class RemovePhoto(_PhotoCommand):
    """Moves a photo to the Trash; undo puts it back in its old place."""

    def __init__(self, service, entry_id, photo_id: str, **kw):
        super().__init__(service, entry_id, "Remove Photo", **kw)
        self.photo_id = photo_id
        self.trash_id: str | None = None
        self.order: list[str] = []

    def apply(self) -> None:
        self.order = self._ids()
        self.trash_id = self._take(self.photo_id)

    def revert(self) -> None:
        self._give_back(self.photo_id, self.trash_id)
        self._order(self.order)


class ReorderPhotos(_PhotoCommand):
    def __init__(self, service, entry_id, old: list[str], new: list[str], **kw):
        super().__init__(service, entry_id, "Reorder Photos", **kw)
        self.old, self.new = list(old), list(new)

    def apply(self) -> None:
        self._order(self.new)

    def revert(self) -> None:
        self._order(self.old)


class ChooseFace(_PhotoCommand):
    """Which face of a photo is used for matching (None: the photo is not used). Made by a background
    task, because finding the face takes a moment."""

    def __init__(self, service, entry_id, photo_id: str, old: Face | None, new: Face | None, **kw):
        text = "Stop Using Photo" if new is None else "Use Photo" if old is None else "Change Face"
        super().__init__(service, entry_id, text, done_already=True, **kw)
        self.photo_id, self.old, self.new = photo_id, old, new

    def apply(self) -> None:
        self.service.choose_face(self.entry_id, self.photo_id, self.new)

    def revert(self) -> None:
        self.service.choose_face(self.entry_id, self.photo_id, self.old)


class SetPhotoNote(_PhotoCommand):
    def __init__(self, service, entry_id, photo_id: str, old: str, new: str, **kw):
        super().__init__(service, entry_id, "Edit Photo Note", **kw)
        self.photo_id, self.old, self.new = photo_id, old, new

    def apply(self) -> None:
        self.service.set_photo_note(self.entry_id, self.photo_id, self.new)

    def revert(self) -> None:
        self.service.set_photo_note(self.entry_id, self.photo_id, self.old)


# --- entries ------------------------------------------------------------------------------------

class _EntryCommand(LibraryCommand):
    def _delete(self) -> str:
        return self.service.delete_entry(self.entry_id).id

    def _bring_back(self, trash_id: str | None) -> None:
        if self.service.has_entry(self.entry_id):         # restored from the Trash page already
            return
        if trash_id is None or not self.service.in_trash(trash_id):
            raise LookupError("the entry is no longer in the Trash")
        self.service.restore(trash_id)


class NewEntry(_EntryCommand):
    """An entry that was just created. Undo moves it to the Trash (nothing is ever deleted for good
    without asking); redo restores it."""

    def __init__(self, service, entry_id, **kw):
        super().__init__(service, entry_id, "New Entry", done_already=True, **kw)
        self.trash_id: str | None = None

    def revert(self) -> None:
        self.trash_id = self._delete()

    def apply(self) -> None:
        self._bring_back(self.trash_id)


class DeleteEntry(_EntryCommand):
    def __init__(self, service, entry_id, **kw):
        super().__init__(service, entry_id, "Delete Entry", **kw)
        self.trash_id: str | None = None

    def apply(self) -> None:
        self.trash_id = self._delete()

    def revert(self) -> None:
        self._bring_back(self.trash_id)
