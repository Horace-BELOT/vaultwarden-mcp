"""Bitwarden CLI boundary: the JSON models `bw` prints, and the subprocess adapter.

The models ignore unknown fields: the CLI adds some across versions and we only read a few.
"""

import os
import shutil
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from vaultwarden_mcp.errors import BitwardenCliError

# Name of the one-shot variable that carries the master password to `bw unlock --passwordenv`.
_PASSWORD_ENV = "VAULTWARDEN_MCP_MASTER_PASSWORD"
_TIMEOUT_SECONDS = 120


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, populate_by_name=True, extra="ignore")


class BwStatus(_Model):
    """Output of `bw status`."""

    status: Literal["unauthenticated", "locked", "unlocked"]
    server_url: str | None = Field(default=None, alias="serverUrl")
    user_email: str | None = Field(default=None, alias="userEmail")
    last_sync: str | None = Field(default=None, alias="lastSync")


class BwUri(_Model):
    """One URI of a login item."""

    uri: str | None = None


class BwLogin(_Model):
    """Login section of an item."""

    username: str | None = None
    password: str | None = None
    totp: str | None = None
    uris: tuple[BwUri, ...] | None = None


class BwField(_Model):
    """Custom field of an item (type 0 text, 1 hidden, 2 boolean, 3 linked)."""

    name: str | None = None
    value: str | None = None
    type: int = 0


class BwItem(_Model):
    """An item of `bw list items` / `bw get item`, decrypted by the CLI."""

    id: str
    type: int
    name: str = ""
    notes: str | None = None
    folder_id: str | None = Field(default=None, alias="folderId")
    collection_ids: tuple[str, ...] | None = Field(default=None, alias="collectionIds")
    login: BwLogin | None = None
    custom_fields: tuple[BwField, ...] | None = Field(default=None, alias="fields")
    card: Mapping[str, str | None] | None = None
    ssh_key: Mapping[str, str | None] | None = Field(default=None, alias="sshKey")
    revision_date: str | None = Field(default=None, alias="revisionDate")


class BwNamed(_Model):
    """A folder or a collection: only its id and name matter here."""

    id: str | None = None
    name: str


ITEMS = TypeAdapter(tuple[BwItem, ...])
NAMED = TypeAdapter(tuple[BwNamed, ...])


def find_bw(explicit: str | None, path: str | None) -> Path | None:
    """Locate the `bw` executable: explicit path first, then the given PATH."""
    if explicit:
        return Path(explicit)
    found = shutil.which("bw", path=path)
    return Path(found) if found else None


class BitwardenCli:
    """Runs the `bw` CLI. Satisfies `vaultwarden_mcp.vault.BwRunner`."""

    def __init__(self, executable: Path, base_env: Mapping[str, str]) -> None:
        """Build an adapter around one `bw` executable and a base environment."""
        self._executable = executable
        # A BW_SESSION inherited from the user's shell would bypass our own unlock step.
        env = {k: v for k, v in base_env.items() if k not in {"BW_SESSION", _PASSWORD_ENV}}
        # `bw` is a Node script: keep its own folder on PATH so its interpreter is found
        # even when the MCP client starts us with a minimal PATH.
        path = env.get("PATH", "")
        env["PATH"] = os.pathsep.join(p for p in (str(executable.parent), path) if p)
        env["BW_NOINTERACTION"] = "true"
        self._env = env

    def run(self, args: Sequence[str], *, session: str | None = None) -> str:
        """Run `bw <args>` and return its stdout."""
        env = dict(self._env)
        if session:
            env["BW_SESSION"] = session
        return self._exec(args, env)

    def unlock(self, password: str) -> str:
        """Unlock with the master password (passed by environment, never argv) -> session key."""
        env = dict(self._env)
        env[_PASSWORD_ENV] = password
        return self._exec(["unlock", "--raw", "--passwordenv", _PASSWORD_ENV], env).strip()

    def _exec(self, args: Sequence[str], env: Mapping[str, str]) -> str:
        command = args[0] if args else ""
        if not self._executable.exists():
            raise BitwardenCliError(command, f"{self._executable} not found")
        try:
            proc = subprocess.run(
                [str(self._executable), *args],
                capture_output=True,
                text=True,
                env=env,
                timeout=_TIMEOUT_SECONDS,
                check=False,
            )
        except subprocess.TimeoutExpired as e:
            raise BitwardenCliError(command, f"timed out after {_TIMEOUT_SECONDS}s") from e
        if proc.returncode != 0:
            lines = (proc.stderr or proc.stdout).strip().splitlines()
            raise BitwardenCliError(command, lines[-1] if lines else f"exit {proc.returncode}")
        return proc.stdout
