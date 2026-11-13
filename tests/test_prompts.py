import os
import shlex
import subprocess
import sys

import pytest

from vaultwarden_mcp.errors import UnlockError, ValueSourceError
from vaultwarden_mcp.prompts import (
    CommandPrompt,
    LinuxDialog,
    MacDialog,
    WindowsDialog,
    native_prompt,
)

ENV = dict(os.environ)  # Windows cannot start a process without SystemRoot & co.


def _python(code: str) -> str:
    argv = [sys.executable, "-c", code]
    return subprocess.list2cmdline(argv) if os.name == "nt" else shlex.join(argv)


def test_command_prompt_returns_stdout_without_the_trailing_newline() -> None:
    prompt = CommandPrompt(_python("print('pw with spaces')"), ENV)
    assert prompt.ask("me@example.test") == "pw with spaces"


def test_command_prompt_sees_the_account_message_in_its_environment() -> None:
    code = "import os; print('me@example.test' in os.environ['VAULTWARDEN_MCP_PROMPT'])"
    assert CommandPrompt(_python(code), ENV).ask("me@example.test") == "True"


def test_command_prompt_failure_is_an_unlock_error() -> None:
    with pytest.raises(UnlockError, match="failed: boom"):
        CommandPrompt(_python("import sys; sys.exit('boom')"), ENV).ask("me")


def test_command_prompt_with_a_missing_executable_is_an_unlock_error() -> None:
    with pytest.raises(UnlockError, match="not found"):
        CommandPrompt("/nonexistent/vwmcp-password-helper", ENV).ask("me")


def test_linux_dialog_without_a_graphical_session_points_to_password_command() -> None:
    with pytest.raises(UnlockError, match="--password-command"):
        LinuxDialog({"PATH": "/usr/bin"}).ask("me")


@pytest.mark.parametrize(
    ("platform", "expected"),
    [("darwin", MacDialog), ("win32", WindowsDialog), ("linux", LinuxDialog)],
)
def test_native_prompt_picks_the_dialog_of_the_platform(platform: str, expected: type) -> None:
    assert isinstance(native_prompt(platform, {}), expected)


def test_value_dialog_without_a_graphical_session_is_a_value_source_error() -> None:
    with pytest.raises(ValueSourceError, match="generate"):
        LinuxDialog({"PATH": "/usr/bin"}).ask_value("store it")
