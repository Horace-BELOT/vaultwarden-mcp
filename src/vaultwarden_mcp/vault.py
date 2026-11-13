"""Vault session: unlocks on demand, keeps the session key in memory only, locks when idle.

The session key never leaves this object: it is not written to disk, not exported to the
environment of other processes, and not returned by any tool.
"""

import base64
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from vaultwarden_mcp.bitwarden import COLLECTIONS, ITEMS, NAMED, BwItem, BwStatus
from vaultwarden_mcp.errors import BitwardenCliError, NotLoggedInError, UnlockError
from vaultwarden_mcp.logs import Log


class BwRunner(Protocol):
    """What the vault needs from the Bitwarden CLI."""

    def run(
        self, args: Sequence[str], *, session: str | None = None, stdin: str | None = None
    ) -> str:
        """Run `bw <args>` (optionally feeding `stdin`) and return stdout."""
        ...

    def unlock(self, password: str) -> str:
        """Unlock with the master password and return the session key."""
        ...


class PasswordPrompt(Protocol):
    """Asks the human for the master password, outside of the MCP conversation."""

    def ask(self, account: str) -> str:
        """Return the master password typed by the user; raise UnlockError if cancelled."""
        ...


@dataclass(frozen=True, slots=True, kw_only=True)
class VaultSettings:
    """Timing knobs, from the command line."""

    idle_lock_seconds: float
    cache_seconds: float


@dataclass(frozen=True, slots=True, kw_only=True)
class Snapshot:
    """Decrypted items with folder and collection names, as of the last load."""

    items: tuple[BwItem, ...]
    folders: Mapping[str, str]
    collections: Mapping[str, str]
    collection_orgs: Mapping[str, str]
    organizations: Mapping[str, str]


class Vault:
    """Owns the `bw` session for the lifetime of the MCP server process."""

    def __init__(
        self,
        *,
        bw: BwRunner,
        prompt: PasswordPrompt,
        clock: Callable[[], float],
        settings: VaultSettings,
        log: Log,
    ) -> None:
        """Wire the vault to its ports; nothing is unlocked until a tool needs it."""
        self._bw = bw
        self._prompt = prompt
        self._clock = clock
        self._settings = settings
        self._log = log
        self._session: str | None = None
        self._last_use = 0.0
        self._snapshot: Snapshot | None = None
        self._loaded_at = 0.0

    @property
    def unlocked(self) -> bool:
        """Whether this process currently holds a session key."""
        return self._session is not None

    def status(self) -> BwStatus:
        """CLI status (server, account, lock state). Never prompts."""
        return BwStatus.model_validate_json(self._bw.run(["status"], session=self._session))

    def snapshot(self) -> Snapshot:
        """Items, folders and collections; reloaded when older than the cache window."""
        session = self._session_or_unlock()
        if self._snapshot is None or self._clock() - self._loaded_at > self._settings.cache_seconds:
            self._snapshot = self._load(session)
            self._loaded_at = self._clock()
        return self._snapshot

    def item(self, item_id: str) -> BwItem:
        """One item, fetched fresh from the CLI by its exact id."""
        session = self._session_or_unlock()
        return BwItem.model_validate_json(self._bw.run(["get", "item", item_id], session=session))

    def item_json(self, item_id: str) -> dict[str, Any]:
        """One item as the raw JSON of `bw get item`, every field kept, for `edit_item`."""
        session = self._session_or_unlock()
        data: dict[str, Any] = json.loads(self._bw.run(["get", "item", item_id], session=session))
        return data

    def create_item(self, data: Mapping[str, Any]) -> BwItem:
        """`bw create item`; the encoded JSON goes through stdin, never argv."""
        session = self._session_or_unlock()
        out = self._bw.run(["create", "item"], session=session, stdin=_encode(data))
        self._snapshot = None
        return BwItem.model_validate_json(out)

    def edit_item(self, item_id: str, data: Mapping[str, Any]) -> BwItem:
        """`bw edit item`: replaces the item with `data` (a modified `item_json`)."""
        session = self._session_or_unlock()
        out = self._bw.run(["edit", "item", item_id], session=session, stdin=_encode(data))
        self._snapshot = None
        return BwItem.model_validate_json(out)

    def totp(self, item_id: str) -> str:
        """Current TOTP code of an item."""
        session = self._session_or_unlock()
        return self._bw.run(["get", "totp", item_id], session=session).strip()

    def sync(self) -> Snapshot:
        """Pull the vault from the server, then reload the snapshot."""
        session = self._session_or_unlock()
        self._bw.run(["sync"], session=session)
        self._snapshot = self._load(session)
        self._loaded_at = self._clock()
        return self._snapshot

    def lock(self, reason: str) -> None:
        """Forget the session key and the decrypted cache."""
        if self._session is None:
            return
        try:
            self._bw.run(["lock"], session=self._session)
        finally:
            self._session = None
            self._snapshot = None
            self._log.event("vault.locked", reason=reason)

    def _session_or_unlock(self) -> str:
        now = self._clock()
        if self._session is not None and now - self._last_use > self._settings.idle_lock_seconds:
            self.lock("idle")
        if self._session is None:
            self._session = self._unlock()
            self._bw.run(["sync"], session=self._session)
            self._snapshot = None
        self._last_use = now
        return self._session

    def _unlock(self) -> str:
        status = self.status()
        if status.status == "unauthenticated":
            raise NotLoggedInError(
                "The Bitwarden CLI is not logged in. In a terminal: "
                "`bw config server <url>` then `bw login` (see README)."
            )
        password = self._prompt.ask(status.user_email or "")
        try:
            session = self._bw.unlock(password)
        except BitwardenCliError as e:
            raise UnlockError(f"Unlock failed (wrong master password?): {e}") from e
        if not session:
            raise UnlockError("Unlock returned no session key.")
        self._log.event("vault.unlocked", account=status.user_email)
        return session

    def _load(self, session: str) -> Snapshot:
        items = ITEMS.validate_json(self._bw.run(["list", "items"], session=session))
        folders = NAMED.validate_json(self._bw.run(["list", "folders"], session=session))
        collections = COLLECTIONS.validate_json(
            self._bw.run(["list", "collections"], session=session)
        )
        orgs = NAMED.validate_json(self._bw.run(["list", "organizations"], session=session))
        return Snapshot(
            items=items,
            folders={f.id: f.name for f in folders if f.id},
            collections={c.id: c.name for c in collections},
            collection_orgs={c.id: c.organization_id for c in collections},
            organizations={o.id: o.name for o in orgs if o.id},
        )


def _encode(data: Mapping[str, Any]) -> str:
    """What `bw encode` does: base64 of the JSON."""
    return base64.b64encode(json.dumps(data).encode()).decode()
