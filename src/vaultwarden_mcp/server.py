"""MCP tools. Only `get_secret_to_file` touches a secret value, and it never returns it.

`get_secret_to_file` carries `_meta["anthropic/requiresUserInteraction"] = true`: Claude Code
then asks the human on every call, in every permission mode, without a "don't ask again"
option, and allow rules cannot skip it.
"""

from collections.abc import Callable
from typing import Literal

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import BaseModel, ConfigDict

from vaultwarden_mcp import catalog, writer
from vaultwarden_mcp.errors import VaultwardenMcpError
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


def build_server(
    vault: Vault,
    *,
    log: Log,
    search_notes: bool,
    idle_lock_minutes: int,
    git_unignored: writer.GitGuard = writer.git_unignored,
) -> FastMCP:
    """Register the tools on a new FastMCP server bound to `vault`."""
    # WARNING keeps stderr to our own JSON audit lines (FastMCP logs every request at INFO).
    server = FastMCP("vault", instructions=INSTRUCTIONS, log_level="WARNING")

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
