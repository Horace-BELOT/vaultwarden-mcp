import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from vaultwarden_mcp.errors import DestinationError
from vaultwarden_mcp.writer import (
    Destination,
    dotenv_quote,
    git_unignored,
    parse_destination,
    write_secret,
)


def _never_tracked(_: Path) -> bool:
    return False


def _parse(
    path: Path | str,
    *,
    env_key: str | None = None,
    overwrite: bool = False,
    allow_not_gitignored: bool = False,
) -> Destination:
    return parse_destination(
        str(path),
        env_key=env_key,
        overwrite=overwrite,
        allow_not_gitignored=allow_not_gitignored,
        git_unignored=_never_tracked,
    )


POSIX = os.name != "nt"  # Windows has no POSIX modes: only check them where they exist


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_raw_write_creates_a_private_file(tmp_path: Path) -> None:
    target = tmp_path / "token"
    written = write_secret(_parse(target), "value-1")
    assert target.read_text() == "value-1"
    assert not POSIX or _mode(target) == 0o600
    assert (written.mode, written.length) == ("raw", 7)


def test_raw_write_refuses_an_existing_file_without_overwrite(tmp_path: Path) -> None:
    target = tmp_path / "token"
    target.write_text("old")
    with pytest.raises(DestinationError, match="already exists"):
        _parse(target)
    write_secret(_parse(target, overwrite=True), "new")
    assert target.read_text() == "new"


def test_env_write_adds_the_key_to_a_new_private_file(tmp_path: Path) -> None:
    target = tmp_path / ".env"
    written = write_secret(_parse(target, env_key="API_KEY"), "abc")
    assert target.read_text() == "API_KEY='abc'\n"
    assert not POSIX or _mode(target) == 0o600
    assert written.mode == "env_added"


def test_env_write_replaces_the_key_and_keeps_other_lines_and_mode(tmp_path: Path) -> None:
    target = tmp_path / ".env"
    target.write_text("# comment\nexport API_KEY=old\nOTHER=1\n")
    target.chmod(0o640)
    written = write_secret(_parse(target, env_key="API_KEY"), "new")
    assert target.read_text() == "# comment\nexport API_KEY='new'\nOTHER=1\n"
    assert not POSIX or _mode(target) == 0o640
    assert written.mode == "env_replaced"


def test_dotenv_quote_uses_single_quotes_unless_the_value_needs_escaping() -> None:
    assert dotenv_quote('a"b$c') == "'a\"b$c'"
    assert dotenv_quote("it's\nmultiline") == '"it\'s\\nmultiline"'


@pytest.mark.parametrize(
    ("destination", "env_key", "message"),
    [
        ("relative/path", None, "absolute path"),
        ("{tmp}/missing-folder/x", None, "does not exist"),
        ("{tmp}/.env", "1BAD", "Invalid env_key"),
        ("{tmp}", None, "is a folder"),
    ],
)
def test_parse_destination_refuses_bad_destinations(
    tmp_path: Path, destination: str, env_key: str | None, message: str
) -> None:
    with pytest.raises(DestinationError, match=message):
        _parse(destination.format(tmp=tmp_path), env_key=env_key)


def test_parse_destination_refuses_a_file_git_would_track(tmp_path: Path) -> None:
    with pytest.raises(DestinationError, match="not ignored"):
        parse_destination(
            str(tmp_path / ".env"),
            env_key="K",
            overwrite=False,
            allow_not_gitignored=False,
            git_unignored=lambda _: True,
        )


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_git_unignored_tells_tracked_from_ignored_files(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / ".gitignore").write_text(".env\n")
    assert git_unignored(repo / "config.txt")
    assert not git_unignored(repo / ".env")
    assert not git_unignored(tmp_path / "outside-any-repo.txt")
