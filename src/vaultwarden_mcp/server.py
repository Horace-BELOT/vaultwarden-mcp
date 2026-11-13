"""MCP tools. No tool ever returns a secret value.

Only `get_secret_to_file` reads a secret, and only `create_secret` / `update_secret` write
one (registered with `--allow-writes`). These three carry
`_meta["anthropic/requiresUserInteraction"] = true`: Claude Code then asks the human on every
call, in every permission mode, without a "don't ask again" option, and allow rules cannot
skip it.
"""

from collections.abc import Callable
from typing import Any, Literal

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import BaseModel, ConfigDict

from vaultwarden_mcp import catalog, editor, writer
from vaultwarden_mcp.errors import TargetError, ValueSourceError, VaultwardenMcpError
from vaultwarden_mcp.logs import Log
from vaultwarden_mcp.vault import Vault

REQUIRES_USER_INTERACTION = {"anthropic/requiresUserInteraction": True}

INSTRUCTIONS = """\
Access to the user's Vaultwarden vault.
- search_secrets finds items by name, username, URL, folder, collection, custom field name
  or note content. It returns metadata only; use it first to get the item id and the exact
  field name (`available_fields`).
- get_secret_to_file writes ONE value to a file or a dotenv key. The user approves each call.
  Prefer a dotenv key of the file the program reads (env_key), or a path the program takes.
- Never read, print, cat, grep or copy a file written by get_secret_to_file, and never ask
  the user to paste a secret in the conversation.
"""

WRITE_INSTRUCTIONS = """\
- create_secret / update_secret store ONE value in the vault. The user approves each call.
  The value never goes through you: source "generate" (random password), "dialog" (the user
  types it in a window) or "file" (a file a program wrote, e.g. a token printed by a CLI and
  redirected to a file). Never write a secret into a file yourself to pass it here.
- Pick the collection like the neighbouring items (search_secrets shows their collections;
  list_folders_and_collections lists them all). Search first: update an existing item rather
  than creating a duplicate.
- update_secret only changes the vault, not the service the secret belongs to.
"""


class _Result(BaseModel):
    """Tool results are the wire contract: pydantic gives FastMCP their output schema."""

    model_config = ConfigDict(frozen=True)


class StatusResult(_Result):
    """Lock state as seen by this server."""

    server: str | None
    account: str | None
    cli_status: str
    unlocked_here: bool
    last_sync: str | None
    idle_lock_minutes: int


class SearchResult(_Result):
    """Matching items, metadata only."""

    total: int
    items: tuple[catalog.ItemSummary, ...]


class WriteResult(_Result):
    """Where a value was written; the value itself is not included."""

    item: str
    field: str
    destination: str
    mode: Literal["raw", "env_added", "env_replaced"]
    length: int
    note: str


class FileCopy(_Result):
    """Where a stored value was also written; the value itself is not included."""

    destination: str
    mode: Literal["raw", "env_added", "env_replaced"]


class CreateResult(_Result):
    """The new item, without the value."""

    item_id: str
    name: str
    collections: tuple[str, ...]
    folder: str | None
    field: str
    source: str
    length: int
    also_written_to: FileCopy | None


class UpdateResult(_Result):
    """The edited item, without the value."""

    item_id: str
    name: str
    field: str
    source: str
    length: int
    previous_value_in_history: bool | None
    also_written_to: FileCopy | None
    note: str


class Collection(_Result):
    """A collection and the organization that owns it."""

    name: str
    organization: str | None


class LocationsResult(_Result):
    """Where items can be stored."""

    folders: tuple[str, ...]
    collections: tuple[Collection, ...]


class SyncResult(_Result):
    """Item count after a sync."""

    items: int


class LockResult(_Result):
    """Confirmation that the session was dropped."""

    locked: bool


def _guarded[T](fn: Callable[[], T]) -> T:
    """Turn our errors into MCP tool errors; anything else is a bug and propagates."""
    try:
        return fn()
    except VaultwardenMcpError as e:
        raise ToolError(str(e)) from e


def _copy(target: writer.Destination | None, value: str, item_id: str) -> FileCopy | None:
    """Also write the stored value to a file. The item already exists at this point."""
    if target is None:
        return None
    try:
        written = writer.write_secret(target, value)
    except OSError as e:
        raise TargetError(
            f"The vault was updated (item {item_id}), but writing {target.path} failed: {e}. "
            "Use get_secret_to_file to write it."
        ) from e
    return FileCopy(destination=str(written.path), mode=written.mode)


def _destination(
    destination: str | None,
    *,
    env_key: str | None,
    overwrite: bool,
    allow_not_gitignored: bool,
    git_unignored: writer.GitGuard,
) -> writer.Destination | None:
    if destination is None:
        if env_key is not None:
            raise TargetError("env_key needs a destination.")
        return None
    return writer.parse_destination(
        destination,
        env_key=env_key,
        overwrite=overwrite,
        allow_not_gitignored=allow_not_gitignored,
        git_unignored=git_unignored,
    )


def _history(data: dict[str, Any]) -> list[dict[str, Any]]:
    return [dict(h) for h in data.get("passwordHistory") or []]


def build_server(  # noqa: PLR0913 -- options of the composition root
    vault: Vault,
    *,
    log: Log,
    search_notes: bool,
    idle_lock_minutes: int,
    git_unignored: writer.GitGuard = writer.git_unignored,
    allow_writes: bool = False,
    value_prompt: editor.ValuePrompt | None = None,
) -> FastMCP:
    """Register the tools on a new FastMCP server bound to `vault`.

    The write tools exist only with `allow_writes`; `value_prompt` serves source "dialog".
    """
    # WARNING keeps stderr to our own JSON audit lines (FastMCP logs every request at INFO).
    instructions = INSTRUCTIONS + (WRITE_INSTRUCTIONS if allow_writes else "")
    server = FastMCP("vault", instructions=instructions, log_level="WARNING")

    @server.tool(
        structured_output=True, annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False)
    )
    def vault_status() -> StatusResult:
        """Server, account and lock state. Never opens the unlock dialog."""
        status = _guarded(vault.status)
        return StatusResult(
            server=status.server_url,
            account=status.user_email,
            cli_status=status.status,
            unlocked_here=vault.unlocked,
            last_sync=status.last_sync,
            idle_lock_minutes=idle_lock_minutes,
        )

    @server.tool(
        structured_output=True, annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False)
    )
    def search_secrets(query: str = "", limit: int = 30) -> SearchResult:
        """Search vault items; every word of `query` must match (case and accents ignored).

        Searched: name, username, URIs, folder, collections, custom field names, and note
        content when enabled. Empty query lists everything. Returns metadata only, never a
        password, field value or note. The first call opens the unlock dialog on the
        user's screen.
        """
        snapshot = _guarded(vault.snapshot)
        found = catalog.search(
            snapshot.items,
            query,
            snapshot.folders,
            snapshot.collections,
            search_notes=search_notes,
        )
        return SearchResult(total=len(found), items=tuple(found[: max(limit, 0)]))

    @server.tool(
        structured_output=True,
        annotations=ToolAnnotations(
            readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False
        ),
        meta=REQUIRES_USER_INTERACTION,
    )
    def get_secret_to_file(  # noqa: PLR0913 -- the model sees one flat argument list
        *,
        item_id: str,
        field: str,
        destination: str,
        reason: str,
        env_key: str | None = None,
        overwrite: bool = False,
        allow_not_gitignored: bool = False,
    ) -> WriteResult:
        """Write ONE secret value to a file without returning it. The user approves each call.

        Args:
            item_id: `id` from search_secrets.
            field: one of the item's `available_fields` (password, username, totp, notes,
                field:<name>, card.<key>, sshKey.<key>).
            destination: absolute path (or ~/...); its folder must exist.
            reason: why this secret is needed; shown to the user in the approval prompt.
            env_key: write or replace the line `ENV_KEY='value'` in a dotenv file instead of
                writing the raw value.
            overwrite: raw mode only; allow replacing an existing file.
            allow_not_gitignored: by default a file inside a git repository must be
                ignored by git.

        Do not read the file afterwards: hand it to the program that needs it.
        """

        def run() -> WriteResult:
            # Checked before touching the vault: a refused path never prompts for a password.
            target = writer.parse_destination(
                destination,
                env_key=env_key,
                overwrite=overwrite,
                allow_not_gitignored=allow_not_gitignored,
                git_unignored=git_unignored,
            )
            item = vault.item(item_id)
            value = (
                vault.totp(item.id) if field == catalog.TOTP else catalog.extract_field(item, field)
            )
            written = writer.write_secret(target, value)
            log.event(
                "secret.written",
                item_id=item.id,
                item=item.name,
                field=field,
                destination=str(written.path),
                mode=written.mode,
                reason=reason,
            )
            return WriteResult(
                item=item.name,
                field=field,
                destination=str(written.path),
                mode=written.mode,
                length=written.length,
                note="The value is not returned. Do not read this file; use it.",
            )

        return _guarded(run)

    @server.tool(
        structured_output=True, annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False)
    )
    def list_folders_and_collections() -> LocationsResult:
        """Folders and collections (with their organization) where items can be stored."""
        snapshot = _guarded(vault.snapshot)
        return LocationsResult(
            folders=tuple(sorted(snapshot.folders.values(), key=catalog.normalize)),
            collections=tuple(
                Collection(
                    name=name,
                    organization=snapshot.organizations.get(snapshot.collection_orgs[cid]),
                )
                for cid, name in sorted(
                    snapshot.collections.items(), key=lambda kv: catalog.normalize(kv[1])
                )
            ),
        )

    if allow_writes:
        _register_writes(server, vault, log=log, git_unignored=git_unignored, prompt=value_prompt)

    @server.tool(
        structured_output=True, annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=True)
    )
    def sync_vault() -> SyncResult:
        """Pull the latest vault from the Vaultwarden server."""
        return SyncResult(items=len(_guarded(vault.sync).items))

    @server.tool(
        structured_output=True, annotations=ToolAnnotations(readOnlyHint=False, idempotentHint=True)
    )
    def lock_vault() -> LockResult:
        """Lock the vault now; the next call will ask for the master password again."""
        _guarded(lambda: vault.lock("tool"))
        return LockResult(locked=True)

    return server


class _NoDialog:
    """Stands in for the value dialog when the server has none."""

    def ask_value(self, message: str) -> str:
        """Refuse: there is no window to show `message` in."""
        raise ValueSourceError('No dialog available: use source "generate" or "file".')


def _register_writes(
    server: FastMCP,
    vault: Vault,
    *,
    log: Log,
    git_unignored: writer.GitGuard,
    prompt: editor.ValuePrompt | None,
) -> None:
    """`create_secret` and `update_secret`: they write to the vault, never delete."""
    dialog = prompt if prompt is not None else _NoDialog()

    @server.tool(
        structured_output=True,
        annotations=ToolAnnotations(
            readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True
        ),
        meta=REQUIRES_USER_INTERACTION,
    )
    def create_secret(  # noqa: PLR0913 -- the model sees one flat argument list
        *,
        name: str,
        reason: str,
        source: editor.Source,
        field: str = "password",
        item_type: editor.ItemKind = "login",
        username: str | None = None,
        uris: list[str] | None = None,
        collections: list[str] | None = None,
        folder: str | None = None,
        source_path: str | None = None,
        length: int = 32,
        special_chars: bool = False,
        destination: str | None = None,
        env_key: str | None = None,
        overwrite: bool = False,
        allow_not_gitignored: bool = False,
        allow_duplicate_name: bool = False,
    ) -> CreateResult:
        """Create a vault item holding ONE secret value. The user approves each call.

        The value never goes through the model. Args:
            name: item name, in the style of the neighbouring items.
            reason: why; shown to the user in the approval prompt.
            source: "generate" (random password), "dialog" (the user types or pastes it in
                a window, one line) or "file" (read from source_path; one trailing newline
                dropped; several lines only for notes).
            field: where the value goes: password (login items), notes, or field:<name> (a
                hidden custom field).
            item_type: login or secure_note.
            username, uris: login items only; not secret.
            collections: collection names (or ids) from search_secrets or
                list_folders_and_collections; empty = the user's personal vault.
            folder: folder name (personal folders), optional.
            length, special_chars: for source "generate".
            destination, env_key, overwrite, allow_not_gitignored: also write the value to a
                file or a dotenv key, as get_secret_to_file does.
            allow_duplicate_name: by default an item with the same name in the same place is
                refused (update it with update_secret instead).
        """

        def run() -> CreateResult:
            request = editor.ValueRequest(
                source=source, source_path=source_path, length=length, special_chars=special_chars
            )
            if item_type != "login" and (username or uris):
                raise TargetError("username and uris are for login items.")
            editor.check_field(field, item_type)
            editor.check_request(request, field)
            target = _destination(
                destination,
                env_key=env_key,
                overwrite=overwrite,
                allow_not_gitignored=allow_not_gitignored,
                git_unignored=git_unignored,
            )
            if not name.strip():
                raise TargetError("name is empty.")
            snapshot = vault.snapshot()
            placement = editor.place(snapshot, collections or [], folder)
            twins = editor.same_name(snapshot, name, placement)
            if twins and not allow_duplicate_name:
                raise TargetError(
                    f"{name!r} already exists there (id {twins[0].id}): use update_secret, or "
                    "pass allow_duplicate_name=true."
                )
            where = ", ".join(placement.collection_names) or "your personal vault"
            value = editor.obtain_value(
                request,
                field,
                dialog,
                f"vaultwarden-mcp will create «{name}» in {where} and store this value in "
                f"its {field}.\n\nIt goes to the vault only, never to the AI assistant.",
            )
            data = editor.new_item(
                editor.NewItem(
                    name=name, kind=item_type, username=username, uris=tuple(uris or ())
                ),
                placement,
                field,
                value,
            )
            created = vault.create_item(data)
            log.event(
                "secret.created",
                item_id=created.id,
                item=created.name,
                field=field,
                source=source,
                collections=", ".join(placement.collection_names) or None,
                reason=reason,
            )
            return CreateResult(
                item_id=created.id,
                name=created.name,
                collections=placement.collection_names,
                folder=placement.folder_name,
                field=field,
                source=source,
                length=len(value),
                also_written_to=_copy(target, value, created.id),
            )

        return _guarded(run)

    @server.tool(
        structured_output=True,
        annotations=ToolAnnotations(
            readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=True
        ),
        meta=REQUIRES_USER_INTERACTION,
    )
    def update_secret(  # noqa: PLR0913 -- the model sees one flat argument list
        *,
        item_id: str,
        field: str,
        reason: str,
        source: editor.Source,
        source_path: str | None = None,
        length: int = 32,
        special_chars: bool = False,
        destination: str | None = None,
        env_key: str | None = None,
        overwrite: bool = False,
        allow_not_gitignored: bool = False,
    ) -> UpdateResult:
        """Replace ONE value of an existing item. The user approves each call.

        Only the vault changes, not the service the secret belongs to. Args:
            item_id: `id` from search_secrets.
            field: password (login items), notes, or field:<name> (created as a hidden
                field if absent). Bitwarden keeps the previous password and hidden-field
                values in the item's history (5 entries); not notes or text fields.
            reason: why; shown to the user in the approval prompt.
            source, source_path, length, special_chars: as in create_secret.
            destination, env_key, overwrite, allow_not_gitignored: also write the new value
                to a file or a dotenv key, as get_secret_to_file does.
        """

        def run() -> UpdateResult:
            request = editor.ValueRequest(
                source=source, source_path=source_path, length=length, special_chars=special_chars
            )
            editor.check_request(request, field)
            target = _destination(
                destination,
                env_key=env_key,
                overwrite=overwrite,
                allow_not_gitignored=allow_not_gitignored,
                git_unignored=git_unignored,
            )
            data = vault.item_json(item_id)
            editor.check_field(field, editor.kind_of(data))
            item_name = str(data.get("name", item_id))
            value = editor.obtain_value(
                request,
                field,
                dialog,
                f"vaultwarden-mcp will replace the {field} of «{item_name}» with this value."
                "\n\nIt goes to the vault only, never to the AI assistant.",
            )
            had_value = editor.had_value(data, field)
            edited = vault.edit_item(str(data["id"]), editor.set_value(data, field, value))
            after = [dict(h) for h in edited.password_history or ()]
            kept = (after != _history(data)) if had_value else None
            log.event(
                "secret.updated",
                item_id=edited.id,
                item=edited.name,
                field=field,
                source=source,
                previous_value_in_history=kept,
                reason=reason,
            )
            if kept is None:
                note = "The field was empty: nothing to keep."
            elif kept:
                note = "The previous value is in the item's password history."
            elif editor.keeps_history(data, field):
                note = "Bitwarden did not record the previous value in the history."
            else:
                note = f"Bitwarden keeps no history for {field}: the previous value is gone."
            return UpdateResult(
                item_id=edited.id,
                name=edited.name,
                field=field,
                source=source,
                length=len(value),
                previous_value_in_history=kept,
                also_written_to=_copy(target, value, edited.id),
                note=note,
            )

        return _guarded(run)
