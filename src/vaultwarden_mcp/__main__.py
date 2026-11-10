"""Composition root: parse options, build the adapters, run the MCP server on stdio."""

import argparse
import atexit
import os
import sys
import time
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import TextIO

from vaultwarden_mcp.bitwarden import BitwardenCli, find_bw
from vaultwarden_mcp.logs import Log
from vaultwarden_mcp.prompts import CommandPrompt, native_prompt
from vaultwarden_mcp.server import build_server
from vaultwarden_mcp.vault import Vault, VaultSettings


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="vaultwarden-mcp",
        description="MCP server: search a Vaultwarden vault, write secrets to files with "
        "human approval, never return secret values to the model.",
    )
    parser.add_argument(
        "--bw", help="path to the Bitwarden CLI (default: `bw` found on PATH)", default=None
    )
    parser.add_argument(
        "--idle-lock-minutes",
        type=int,
        default=720,
        help="lock the vault after this many idle minutes (default: 720, i.e. 12 h)",
    )
    parser.add_argument(
        "--cache-minutes",
        type=int,
        default=5,
        help="reuse the item list for this many minutes before reloading it (default: 5)",
    )
    parser.add_argument(
        "--no-search-notes",
        action="store_true",
        help="do not search note content (by default notes are searched, never returned)",
    )
    parser.add_argument(
        "--password-command",
        default=None,
        help="command printing the master password, instead of the native dialog "
        "(headless machines, OS keychain)",
    )
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    env: Mapping[str, str] | None = None,
    err: TextIO | None = None,
) -> int:
    """Run the server until the client closes stdin; return the exit code."""
    args = _parser().parse_args(sys.argv[1:] if argv is None else argv)
    env = dict(os.environ) if env is None else env
    err = sys.stderr if err is None else err
    log = Log(err, lambda: datetime.now(UTC))

    bw_path = find_bw(args.bw, env.get("PATH"))
    if bw_path is None:
        log.event("startup.failed", reason="bw not found on PATH; install bitwarden-cli")
        return 2
    prompt = (
        CommandPrompt(args.password_command, env)
        if args.password_command
        else native_prompt(sys.platform, env)
    )
    vault = Vault(
        bw=BitwardenCli(bw_path, env),
        prompt=prompt,
        clock=time.monotonic,
        settings=VaultSettings(
            idle_lock_seconds=args.idle_lock_minutes * 60,
            cache_seconds=args.cache_minutes * 60,
        ),
        log=log,
    )
    atexit.register(vault.lock, "exit")
    server = build_server(
        vault,
        log=log,
        search_notes=not args.no_search_notes,
        idle_lock_minutes=args.idle_lock_minutes,
    )
    log.event("startup.ready", bw=str(bw_path), prompt=type(prompt).__name__)
    server.run("stdio")
    return 0


if __name__ == "__main__":
    sys.exit(main())
