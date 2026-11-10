"""Write one secret value to a file, either raw or as a `KEY=value` line of a dotenv file.

Destinations are checked before the vault is touched, so a refused destination never
prompts for the master password. Writes are atomic (temp file + rename) and new files are
created `0600`.
"""

import os
import re
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from vaultwarden_mcp.errors import DestinationError

_ENV_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_PRIVATE = 0o600

type GitGuard = Callable[[Path], bool]


@dataclass(frozen=True, slots=True, kw_only=True)
class Destination:
    """Where and how to write a secret."""

    path: Path
    env_key: str | None
    overwrite: bool
    allow_not_gitignored: bool


@dataclass(frozen=True, slots=True, kw_only=True)
class Written:
    """What was written, without the value."""

    path: Path
    mode: Literal["raw", "env_added", "env_replaced"]
    length: int


def parse_destination(
    destination: str,
    *,
    env_key: str | None,
    overwrite: bool,
    allow_not_gitignored: bool,
    git_unignored: GitGuard,
) -> Destination:
    """Validate a destination given by the model; raise DestinationError when refused."""
    path = Path(destination).expanduser()
    if not path.is_absolute():
        raise DestinationError("destination must be an absolute path (or start with ~).")
    if not path.parent.is_dir():
        raise DestinationError(f"Folder {path.parent} does not exist.")
    if path.is_dir():
        raise DestinationError(f"{path} is a folder.")
    if env_key is not None and not _ENV_KEY.fullmatch(env_key):
        raise DestinationError(f"Invalid env_key {env_key!r}: letters, digits, _ only.")
    if env_key is None and path.exists() and not overwrite:
        raise DestinationError(f"{path} already exists: pass overwrite=true, or use env_key.")
    if not allow_not_gitignored and git_unignored(path):
        raise DestinationError(
            f"{path} is inside a git repository and not ignored: add it to .gitignore "
            "first, or pass allow_not_gitignored=true."
        )
    return Destination(
        path=path,
        env_key=env_key,
        overwrite=overwrite,
        allow_not_gitignored=allow_not_gitignored,
    )


def write_secret(destination: Destination, value: str) -> Written:
    """Write the value; a dotenv file keeps its other lines and its permissions."""
    path = destination.path
    if destination.env_key is None:
        _atomic_write(path, value, _PRIVATE)
        return Written(path=path, mode="raw", length=len(value))
    lines, replaced = _upsert(
        path.read_text().splitlines() if path.exists() else [],
        destination.env_key,
        value,
    )
    mode = path.stat().st_mode & 0o777 if path.exists() else _PRIVATE
    _atomic_write(path, "\n".join(lines) + "\n", mode)
    return Written(path=path, mode="env_replaced" if replaced else "env_added", length=len(value))


def dotenv_quote(value: str) -> str:
    """Quote a value for dotenv parsers: single quotes when possible (no escaping at all)."""
    if "'" not in value and "\n" not in value:
        return f"'{value}'"
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return f'"{escaped}"'


def _upsert(lines: list[str], key: str, value: str) -> tuple[list[str], bool]:
    pattern = re.compile(rf"(\s*(?:export\s+)?){re.escape(key)}\s*=")
    entry = f"{key}={dotenv_quote(value)}"
    out = list(lines)
    for i, line in enumerate(out):
        match = pattern.match(line)
        if match:
            out[i] = match.group(1) + entry
            return out, True
    out.append(entry)
    return out, False


def _atomic_write(path: Path, content: str, mode: int) -> None:
    # mkstemp creates the file 0600, so the value is never readable by others, even briefly.
    # On Windows chmod only toggles read-only: the folder's NTFS ACLs protect the file.
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", newline="") as f:
            f.write(content)
        Path(tmp).chmod(mode)
        Path(tmp).replace(path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def git_unignored(path: Path) -> bool:
    """True when `path` is inside a git work tree and git does not ignore it."""
    folder = str(path.parent)
    try:
        inside = subprocess.run(
            ["git", "-C", folder, "rev-parse", "--is-inside-work-tree"],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:  # no git installed: nothing can be committed by accident
        return False
    if inside.returncode != 0 or inside.stdout.strip() != "true":
        return False
    ignored = subprocess.run(
        ["git", "-C", folder, "check-ignore", "-q", str(path)],
        capture_output=True,
        check=False,
    )
    return ignored.returncode != 0
