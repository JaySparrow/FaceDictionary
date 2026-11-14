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
from .common import plural

Done = Callable[[str | list[str] | None], None]     # show this entry (or these) after a step (None: no entry)
Failed = Callable[["LibraryCommand", Exception], None]


def merged_order(current: list[str], wanted: list[str]) -> list[str]:
    """`wanted` order for the photos that are still there; photos it does not know go last."""
    present = set(current)
    head = [pid for pid in wanted if pid in present]
    known = set(head)
    return head + [pid for pid in current if pid not in known]


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

    def shown_entry(self) -> str | list[str] | None:
        """The entry (or entries) to show after a step."""
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

    def _take(self, photo_ids: list[str]) -> dict[str, str | None]:
        """Move photos of this entry to the Trash; returns their trash items (None: it was gone already)."""
        entry = self._entry()
        return {pid: self.service.remove_photo(self.entry_id, pid).id if entry.photo(pid) is not None else None
                for pid in photo_ids}

    def _give_back(self, trash: dict[str, str | None]) -> None:
        """Bring photos back from the Trash: all of them or, if any is gone from there, none."""
        entry = self._entry()
        missing = [pid for pid in trash if entry.photo(pid) is None]     # not put back from the Trash page
        if any(trash[pid] is None or not self.service.in_trash(trash[pid]) for pid in missing):
            raise LookupError("the photo is no longer in the Trash" if len(trash) == 1 else
                              "some of the photos are no longer in the Trash")
        for pid in missing:
            self.service.restore(trash[pid])


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
        self.trash = self._take(self.photo_ids)

    def apply(self) -> None:
        self._give_back(self.trash)
        self._order(self.order)


class RemovePhotos(_PhotoCommand):
    """Moves photos to the Trash; undo puts them back in their old places."""

    def __init__(self, service, entry_id, photo_ids: list[str], **kw):
        super().__init__(service, entry_id, f"Remove {plural(len(photo_ids), 'Photo')}", **kw)
        self.photo_ids = list(photo_ids)
        self.trash: dict[str, str | None] = {}
        self.order: list[str] = []

    def apply(self) -> None:
        self.order = self._ids()
        self.trash = self._take(self.photo_ids)

    def revert(self) -> None:
        self._give_back(self.trash)
        self._order(self.order)


class ReorderPhotos(_PhotoCommand):
    def __init__(self, service, entry_id, old: list[str], new: list[str], **kw):
        super().__init__(service, entry_id, "Reorder Photos", **kw)
        self.old, self.new = list(old), list(new)

    def apply(self) -> None:
        self._order(self.new)

    def revert(self) -> None:
        self._order(self.old)


class ChooseFaces(_PhotoCommand):
    """Which face of each photo is used for matching (None: the photo is not used), as (photo id, old,
    new) changes. Made by a background task, because finding faces takes a moment."""

    def __init__(self, service, entry_id, changes: list[tuple[str, Face | None, Face | None]], **kw):
        n = len(changes)
        if all(new is None for _, _, new in changes):
            text = f"Stop Using {plural(n, 'Photo')}"
        elif all(old is None for _, old, _ in changes):
            text = f"Use {plural(n, 'Photo')}"
        else:
            text = f"Change {plural(n, 'Face')}"
        super().__init__(service, entry_id, text, done_already=True, **kw)
        self.changes = list(changes)

    def apply(self) -> None:
        self._set([(pid, new) for pid, _, new in self.changes])

    def revert(self) -> None:
        self._set([(pid, old) for pid, old, _ in reversed(self.changes)])

    def _set(self, faces: list[tuple[str, Face | None]]) -> None:
        entry = self._entry()
        if any(entry.photo(pid) is None for pid, _ in faces):
            raise LookupError("the photo is no longer in the album" if len(faces) == 1 else
                              "some of the photos are no longer in the album")
        for pid, face in faces:
            self.service.choose_face(self.entry_id, pid, face)


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
    def _delete(self, entry_ids: list[str]) -> dict[str, str | None]:
        """Move entries to the Trash; returns their trash items (None: it was gone already)."""
        return {eid: self.service.delete_entry(eid).id if self.service.has_entry(eid) else None
                for eid in entry_ids}

    def _bring_back(self, trash: dict[str, str | None]) -> None:
        """Restore entries from the Trash: all of them or, if any is gone from there, none."""
        missing = [eid for eid in trash if not self.service.has_entry(eid)]   # not restored on the Trash page
        if any(trash[eid] is None or not self.service.in_trash(trash[eid]) for eid in missing):
            raise LookupError("the entry is no longer in the Trash" if len(trash) == 1 else
                              "some of the entries are no longer in the Trash")
        for eid in missing:
            self.service.restore(trash[eid])


class NewEntry(_EntryCommand):
    """An entry that was just created. Undo moves it to the Trash (nothing is ever deleted for good
    without asking); redo restores it."""

    def __init__(self, service, entry_id, **kw):
        super().__init__(service, entry_id, "New Entry", done_already=True, **kw)
        self.trash: dict[str, str | None] = {}

    def revert(self) -> None:
        self.trash = self._delete([self.entry_id])

    def apply(self) -> None:
        self._bring_back(self.trash)


class DeleteEntries(_EntryCommand):
    def __init__(self, service, entry_ids: list[str], **kw):
        super().__init__(service, entry_ids[0], f"Delete {plural(len(entry_ids), 'Entry', 'Entries')}", **kw)
        self.entry_ids = list(entry_ids)
        self.trash: dict[str, str | None] = {}

    def apply(self) -> None:
        self.trash = self._delete(self.entry_ids)

    def revert(self) -> None:
        self._bring_back(self.trash)

    def shown_entry(self) -> list[str] | None:
        return [eid for eid in self.entry_ids if self.service.has_entry(eid)] or None
