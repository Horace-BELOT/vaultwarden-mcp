"""Building items to create or edit, and obtaining the value to store in them.

A value to store never comes from the model: it is generated here, read from a file a
program wrote, or typed by the human in a native dialog. It never comes from another vault
item either, so a write cannot be used to copy a secret into an item other people can read.
"""

import secrets
import string
from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

from vaultwarden_mcp.bitwarden import BwItem
from vaultwarden_mcp.catalog import normalize
from vaultwarden_mcp.errors import TargetError, ValueSourceError
from vaultwarden_mcp.vault import Snapshot

type Source = Literal["generate", "dialog", "file"]
type ItemKind = Literal["login", "secure_note"]

MIN_LENGTH = 12
MAX_LENGTH = 128
MAX_FILE_BYTES = 64 * 1024
_SPECIAL = "!#$%&*+-=?@^_"
_TYPES: Mapping[ItemKind, int] = {"login": 1, "secure_note": 2}
_HIDDEN = 1  # custom field type: Bitwarden keeps previous values of hidden fields


class ValuePrompt(Protocol):
    """Asks the human for one value in a native dialog."""

    def ask_value(self, message: str) -> str:
        """Return the value typed or pasted; raise ValueSourceError if cancelled."""
        ...


@dataclass(frozen=True, slots=True, kw_only=True)
class ValueRequest:
    """Where the value to store comes from, as asked by the model."""

    source: Source
    source_path: str | None
    length: int
    special_chars: bool


@dataclass(frozen=True, slots=True, kw_only=True)
class Placement:
    """Organization, collections and folder of a new item, resolved to ids."""

    organization_id: str | None
    collection_ids: tuple[str, ...]
    folder_id: str | None
    collection_names: tuple[str, ...]
    folder_name: str | None


def check_request(request: ValueRequest, field: str) -> None:
    """Validate the request before the vault is touched; raise ValueSourceError."""
    if request.source == "file" and not request.source_path:
        raise ValueSourceError('source "file" needs source_path.')
    if request.source != "file" and request.source_path:
        raise ValueSourceError('source_path is only used with source "file".')
    if request.source == "generate" and not MIN_LENGTH <= request.length <= MAX_LENGTH:
        raise ValueSourceError(f"length must be between {MIN_LENGTH} and {MAX_LENGTH}.")
    if request.source == "dialog" and field == "notes":
        raise ValueSourceError('notes can span lines: use source "file" for them.')
    if request.source == "file":
        _source_file(request.source_path or "")


def obtain_value(request: ValueRequest, field: str, prompt: ValuePrompt, message: str) -> str:
    """The value to store, from its source. Called only after the target is validated."""
    match request.source:
        case "generate":
            return generate(request.length, special_chars=request.special_chars)
        case "dialog":
            return prompt.ask_value(message)
        case "file":
            path = _source_file(request.source_path or "")
            return read_value(path, keep_newlines=field == "notes")


def generate(length: int, *, special_chars: bool) -> str:
    """Random password with at least one lowercase, uppercase and digit (and special)."""
    classes = [string.ascii_lowercase, string.ascii_uppercase, string.digits]
    if special_chars:
        classes.append(_SPECIAL)
    alphabet = "".join(classes)
    while True:
        value = "".join(secrets.choice(alphabet) for _ in range(length))
        if all(any(c in cls for c in value) for cls in classes):
            return value


def _source_file(source_path: str) -> Path:
    path = Path(source_path).expanduser()
    if not path.is_absolute():
        raise ValueSourceError("source_path must be an absolute path (or start with ~).")
    if not path.is_file():
        raise ValueSourceError(f"{path} is not a file.")
    if path.stat().st_size > MAX_FILE_BYTES:
        raise ValueSourceError(f"{path} is larger than {MAX_FILE_BYTES // 1024} KiB.")
    return path


def read_value(path: Path, *, keep_newlines: bool) -> str:
    """File content; one trailing newline is dropped unless the target is a note."""
    try:
        value = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        raise ValueSourceError(f"Cannot read {path} as UTF-8 text: {e}") from e
    if not keep_newlines:
        value = value.removesuffix("\n").removesuffix("\r")
        if "\n" in value:
            raise ValueSourceError(f"{path} has several lines: only notes can hold that.")
    if not value:
        raise ValueSourceError(f"{path} is empty.")
    return value


def check_field(field: str, kind: str) -> None:
    """Fields a value can be stored in: password (logins), notes, field:<name>."""
    if field == "password":
        if kind != "login":
            raise TargetError("Only login items have a password: use notes or field:<name>.")
        return
    if field == "notes":
        return
    if field.startswith("field:") and field.removeprefix("field:").strip():
        return
    raise TargetError(f"Cannot store into {field!r}: use password, notes or field:<name>.")


def place(snapshot: Snapshot, collections: Sequence[str], folder: str | None) -> Placement:
    """Resolve collection and folder names (case and accents ignored) to ids."""
    collection_ids = tuple(_collection_id(snapshot, c) for c in collections)
    orgs = {snapshot.collection_orgs[c] for c in collection_ids}
    if len(orgs) > 1:
        raise TargetError("The collections belong to different organizations: pick one.")
    folder_id = _folder_id(snapshot, folder) if folder else None
    return Placement(
        organization_id=next(iter(orgs), None),
        collection_ids=collection_ids,
        folder_id=folder_id,
        collection_names=tuple(snapshot.collections[c] for c in collection_ids),
        folder_name=snapshot.folders[folder_id] if folder_id else None,
    )


def _collection_id(snapshot: Snapshot, wanted: str) -> str:
    if wanted in snapshot.collections:
        return wanted
    matches = [c for c, n in snapshot.collections.items() if normalize(n) == normalize(wanted)]
    if len(matches) == 1:
        return matches[0]
    labels = [
        f"{n} ({snapshot.organizations.get(snapshot.collection_orgs[c], '?')})"
        for c, n in snapshot.collections.items()
        if not matches or c in matches
    ]
    problem = "is ambiguous" if matches else "does not exist or is not visible to you"
    raise TargetError(f"Collection {wanted!r} {problem}. Collections: {', '.join(labels)}.")


def _folder_id(snapshot: Snapshot, wanted: str) -> str:
    if wanted in snapshot.folders:
        return wanted
    matches = [f for f, n in snapshot.folders.items() if normalize(n) == normalize(wanted)]
    if len(matches) == 1:
        return matches[0]
    raise TargetError(
        f"Folder {wanted!r} does not exist or is ambiguous. "
        f"Folders: {', '.join(snapshot.folders.values()) or 'none'}."
    )


def same_name(snapshot: Snapshot, name: str, placement: Placement) -> list[BwItem]:
    """Items with this name in the same place (same collections, or personal vault)."""
    return [
        i
        for i in snapshot.items
        if normalize(i.name) == normalize(name)
        and (
            set(i.collection_ids or ()) & set(placement.collection_ids)
            if placement.collection_ids
            else not i.collection_ids
        )
    ]


@dataclass(frozen=True, slots=True, kw_only=True)
class NewItem:
    """Non-secret attributes of an item to create."""

    name: str
    kind: ItemKind
    username: str | None
    uris: tuple[str, ...]


def new_item(item: NewItem, placement: Placement, field: str, value: str) -> dict[str, Any]:
    """JSON for `bw create item`, in the shape of `bw get template item`."""
    data: dict[str, Any] = {
        "organizationId": placement.organization_id,
        "collectionIds": list(placement.collection_ids) or None,
        "folderId": placement.folder_id,
        "type": _TYPES[item.kind],
        "name": item.name,
        "notes": None,
        "favorite": False,
        "fields": [],
        "reprompt": 0,
    }
    if item.kind == "login":
        data["login"] = {
            "uris": [{"match": None, "uri": u} for u in item.uris],
            "username": item.username,
            "password": None,
            "totp": None,
        }
    else:
        data["secureNote"] = {"type": 0}
    return set_value(data, field, value)


def set_value(data: Mapping[str, Any], field: str, value: str) -> dict[str, Any]:
    """Copy of an item's JSON with one field set; a new custom field is hidden."""
    out = deepcopy(dict(data))
    if field == "password":
        out["login"] = {**(out.get("login") or {}), "password": value}
    elif field == "notes":
        out["notes"] = value
    else:
        name = field.removeprefix("field:")
        fields = list(out.get("fields") or [])
        for f in fields:
            if f.get("name") == name:
                f["value"] = value
                break
        else:
            fields.append({"name": name, "value": value, "type": _HIDDEN, "linkedId": None})
        out["fields"] = fields
    return out


def kind_of(data: Mapping[str, Any]) -> str:
    """ "login", "secure_note"... of an item's JSON."""
    return {1: "login", 2: "secure_note", 3: "card", 4: "identity", 5: "ssh_key"}.get(
        int(data.get("type", 0)), "other"
    )


def keeps_history(data: Mapping[str, Any], field: str) -> bool:
    """Whether Bitwarden keeps the previous value of this field in the item's history.

    It does for the login password and for hidden custom fields, not for notes or text
    fields.
    """
    if field == "password":
        return True
    if field.startswith("field:"):
        name = field.removeprefix("field:")
        existing = [f for f in data.get("fields") or [] if f.get("name") == name]
        return not existing or all(f.get("type") == _HIDDEN for f in existing)
    return False


def had_value(data: Mapping[str, Any], field: str) -> bool:
    """Whether the field currently holds a non-empty value."""
    if field == "password":
        return bool((data.get("login") or {}).get("password"))
    if field == "notes":
        return bool(data.get("notes"))
    name = field.removeprefix("field:")
    return any(f.get("name") == name and f.get("value") for f in data.get("fields") or [])
