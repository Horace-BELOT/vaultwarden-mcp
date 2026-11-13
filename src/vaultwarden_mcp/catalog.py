"""Pure functions over decrypted items: summaries without secrets, search, field extraction.

`ItemSummary` is the only shape of an item that ever reaches the model. It is built field
by field from non-secret attributes, so a new secret attribute in the CLI output cannot
leak through it by accident.
"""

import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from vaultwarden_mcp.bitwarden import BwItem
from vaultwarden_mcp.errors import FieldNotFoundError

ITEM_TYPES: Mapping[int, str] = {
    1: "login",
    2: "secure_note",
    3: "card",
    4: "identity",
    5: "ssh_key",
}
TOTP = "totp"


@dataclass(frozen=True, slots=True, kw_only=True)
class ItemSummary:
    """Metadata of an item, safe to show to the model."""

    id: str
    name: str
    type: str
    folder: str | None
    collections: tuple[str, ...]
    username: str | None
    uris: tuple[str, ...]
    custom_field_names: tuple[str, ...]
    available_fields: tuple[str, ...]
    revision_date: str | None
    matched_in: tuple[str, ...]


def uris(item: BwItem) -> tuple[str, ...]:
    """Non-empty URIs of a login item."""
    if item.login is None or item.login.uris is None:
        return ()
    return tuple(u.uri for u in item.login.uris if u.uri)


def available_fields(item: BwItem) -> tuple[str, ...]:
    """Names accepted by `extract_field` (and `totp`) for this item, non-empty only."""
    names: list[str] = []
    if item.login is not None:
        if item.login.username:
            names.append("username")
        if item.login.password:
            names.append("password")
        if item.login.totp:
            names.append(TOTP)
    if item.notes:
        names.append("notes")
    names += [f"field:{f.name}" for f in item.custom_fields or () if f.name and f.value]
    names += [f"card.{k}" for k, v in (item.card or {}).items() if v]
    names += [f"sshKey.{k}" for k, v in (item.ssh_key or {}).items() if v]
    return tuple(names)


def extract_field(item: BwItem, field: str) -> str:
    """Value of one field. `totp` is computed by the CLI, not stored: see `Vault.totp`."""
    value: str | None = None
    if field == "username" and item.login is not None:
        value = item.login.username
    elif field == "password" and item.login is not None:
        value = item.login.password
    elif field == "notes":
        value = item.notes
    elif field.startswith("field:"):
        name = field.removeprefix("field:")
        value = next((f.value for f in item.custom_fields or () if f.name == name), None)
    elif field.startswith("card."):
        value = (item.card or {}).get(field.removeprefix("card."))
    elif field.startswith("sshKey."):
        value = (item.ssh_key or {}).get(field.removeprefix("sshKey."))
    if not value:
        available = ", ".join(available_fields(item)) or "none"
        raise FieldNotFoundError(
            f"Field {field!r} is absent or empty on {item.name!r}. Available: {available}."
        )
    return value


def normalize(text: str) -> str:
    """Lowercase without accents, for matching names typed by the model."""
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(c for c in decomposed if not unicodedata.combining(c)).casefold()


def _haystack(
    item: BwItem,
    folders: Mapping[str, str],
    collections: Mapping[str, str],
    *,
    search_notes: bool,
) -> dict[str, str]:
    parts = {
        "name": item.name,
        "username": (item.login.username if item.login else None) or "",
        "uris": " ".join(uris(item)),
        "folder": folders.get(item.folder_id or "", ""),
        "collections": " ".join(collections.get(c, "") for c in item.collection_ids or ()),
        "custom_field_names": " ".join(f.name or "" for f in item.custom_fields or ()),
    }
    if search_notes:
        parts["notes"] = item.notes or ""
    return {k: normalize(v) for k, v in parts.items()}


def summarize(
    item: BwItem,
    folders: Mapping[str, str],
    collections: Mapping[str, str],
    matched_in: tuple[str, ...] = (),
) -> ItemSummary:
    """Build the model-safe view of an item."""
    return ItemSummary(
        id=item.id,
        name=item.name,
        type=ITEM_TYPES.get(item.type, str(item.type)),
        folder=folders.get(item.folder_id or ""),
        collections=tuple(collections.get(c, c) for c in item.collection_ids or ()),
        username=item.login.username if item.login else None,
        uris=uris(item),
        custom_field_names=tuple(f.name for f in item.custom_fields or () if f.name),
        available_fields=available_fields(item),
        revision_date=item.revision_date,
        matched_in=matched_in,
    )


def search(
    items: Iterable[BwItem],
    query: str,
    folders: Mapping[str, str],
    collections: Mapping[str, str],
    *,
    search_notes: bool,
) -> list[ItemSummary]:
    """Items where every query word appears in some searchable attribute, sorted by name.

    Matching ignores case and accents. Notes are searched (when enabled) but only the
    attribute names that matched are reported, never their content.
    """
    terms = normalize(query).split()
    found: list[ItemSummary] = []
    for item in items:
        hay = _haystack(item, folders, collections, search_notes=search_notes)
        if not all(any(t in v for v in hay.values()) for t in terms):
            continue
        matched = tuple(k for k, v in hay.items() if any(t in v for t in terms))
        found.append(summarize(item, folders, collections, matched))
    return sorted(found, key=lambda s: (normalize(s.name), s.id))
