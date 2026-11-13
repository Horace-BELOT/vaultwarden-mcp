"""Dialogs that take a secret from the human, outside of the MCP conversation.

Master password: each class satisfies `vaultwarden_mcp.vault.PasswordPrompt`. The password
goes from a native dialog (or a command the user configured) straight to this process, then
to `bw` through a one-shot environment variable: it never enters the MCP conversation.

Values to store (`create_secret` / `update_secret` with source "dialog"): the native dialogs
also satisfy `vaultwarden_mcp.editor.ValuePrompt`; the value goes from the dialog to `bw`.

The dialog text and button label are passed through the environment, so no quoting of the
account or item name into AppleScript or PowerShell is needed.
"""

import os
import shlex
import shutil
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from vaultwarden_mcp.errors import UnlockError, ValueSourceError, VaultwardenMcpError

_TEXT_ENV = "VAULTWARDEN_MCP_PROMPT"
_BUTTON_ENV = "VAULTWARDEN_MCP_BUTTON"
_TITLE = "Vaultwarden"

_APPLESCRIPT = """
set msg to system attribute "VAULTWARDEN_MCP_PROMPT"
set btn to system attribute "VAULTWARDEN_MCP_BUTTON"
set answer to display dialog msg default answer "" with hidden answer ¬
    buttons {"Cancel", btn} default button btn cancel button "Cancel" ¬
    with title "Vaultwarden" with icon caution
return text returned of answer
"""

# PowerShell 5.1 ships with every Windows 10/11; WinForms gives a real masked dialog.
_POWERSHELL = r"""
Add-Type -AssemblyName System.Windows.Forms
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$f = New-Object Windows.Forms.Form
$f.Text = 'Vaultwarden'; $f.Width = 440; $f.Height = 190; $f.TopMost = $true
$f.StartPosition = 'CenterScreen'; $f.FormBorderStyle = 'FixedDialog'
$f.MaximizeBox = $false; $f.MinimizeBox = $false
$l = New-Object Windows.Forms.Label; $l.Text = $env:VAULTWARDEN_MCP_PROMPT
$l.SetBounds(12, 10, 400, 60)
$t = New-Object Windows.Forms.TextBox; $t.UseSystemPasswordChar = $true
$t.SetBounds(12, 75, 400, 24)
$ok = New-Object Windows.Forms.Button; $ok.Text = $env:VAULTWARDEN_MCP_BUTTON
$ok.DialogResult = 'OK'
$ok.SetBounds(246, 110, 80, 28)
$no = New-Object Windows.Forms.Button; $no.Text = 'Cancel'; $no.DialogResult = 'Cancel'
$no.SetBounds(332, 110, 80, 28)
$f.AcceptButton = $ok; $f.CancelButton = $no; $f.Controls.AddRange(@($l, $t, $ok, $no))
if ($f.ShowDialog() -eq 'OK') { [Console]::Out.Write($t.Text); exit 0 } else { exit 1 }
"""


def _unlock_message(account: str) -> str:
    return (
        f"vaultwarden-mcp wants to unlock the vault of {account or 'this account'}.\n\n"
        "The master password goes to the Bitwarden CLI only, never to the AI assistant."
    )


@dataclass(frozen=True, slots=True)
class _Ask:
    """What one dialog asks for, and how its failures are reported."""

    message: str
    button: str
    error: type[VaultwardenMcpError]
    what: str  # "Password", "Value": start of the error messages


def _unlock(account: str) -> _Ask:
    return _Ask(_unlock_message(account), "Unlock", UnlockError, "Password")


def _value(message: str) -> _Ask:
    return _Ask(message, "Save", ValueSourceError, "Value")


def _run_dialog(command: str | Sequence[str], ask: _Ask, env: Mapping[str, str]) -> str:
    """Run a dialog command; exit 0 + stdout = the answer, any other exit = cancelled."""
    proc = subprocess.run(
        command if isinstance(command, str) else list(command),
        input="",
        capture_output=True,
        text=True,
        env={**env, _TEXT_ENV: ask.message, _BUTTON_ENV: ask.button},
        check=False,
    )
    if proc.returncode != 0:
        detail = proc.stderr.strip().splitlines()
        if detail and "-128" not in detail[-1]:  # -128 is AppleScript's "user cancelled"
            raise ask.error(f"{ask.what} dialog failed: {detail[-1]}")
        raise ask.error(f"{ask.what} entry cancelled by the user.")
    answer = proc.stdout.removesuffix("\n").removesuffix("\r")
    if not answer:
        raise ask.error(f"Empty {ask.what.lower()}.")
    return answer


class MacDialog:
    """`osascript` dialog with a masked field."""

    def __init__(self, env: Mapping[str, str]) -> None:
        """Keep the environment the dialog process inherits."""
        self._env = env

    def ask(self, account: str) -> str:
        """Show the dialog and return the typed password."""
        return self._show(_unlock(account))

    def ask_value(self, message: str) -> str:
        """Show the dialog and return the typed or pasted value (one line)."""
        return self._show(_value(message))

    def _show(self, ask: _Ask) -> str:
        return _run_dialog(["osascript", "-e", _APPLESCRIPT], ask, self._env)


class LinuxDialog:
    """`zenity --password`, or `kdialog --password` on KDE."""

    def __init__(self, env: Mapping[str, str]) -> None:
        """Keep the environment the dialog process inherits."""
        self._env = env

    def ask(self, account: str) -> str:
        """Show the dialog and return the typed password."""
        return self._show(_unlock(account), "use --password-command instead (see README).")

    def ask_value(self, message: str) -> str:
        """Show the dialog and return the typed or pasted value (one line)."""
        return self._show(_value(message), 'use source "generate" or "file" instead.')

    def _show(self, ask: _Ask, headless_hint: str) -> str:
        if not (self._env.get("DISPLAY") or self._env.get("WAYLAND_DISPLAY")):
            raise ask.error(
                f"No graphical session (DISPLAY/WAYLAND_DISPLAY unset): {headless_hint}"
            )
        if shutil.which("zenity", path=self._env.get("PATH")):
            argv = [
                "sh",
                "-c",
                f'exec zenity --entry --hide-text --title {_TITLE} --text "${_TEXT_ENV}"',
            ]
        elif shutil.which("kdialog", path=self._env.get("PATH")):
            argv = ["sh", "-c", f'exec kdialog --title {_TITLE} --password "${_TEXT_ENV}"']
        else:
            raise ask.error(
                "Neither zenity nor kdialog is installed: install one "
                f"(e.g. `sudo apt install zenity`), or {headless_hint}"
            )
        return _run_dialog(argv, ask, self._env)


class WindowsDialog:
    """WinForms dialog through Windows PowerShell."""

    def __init__(self, env: Mapping[str, str]) -> None:
        """Keep the environment the dialog process inherits."""
        self._env = env

    def ask(self, account: str) -> str:
        """Show the dialog and return the typed password."""
        return self._show(_unlock(account))

    def ask_value(self, message: str) -> str:
        """Show the dialog and return the typed or pasted value (one line)."""
        return self._show(_value(message))

    def _show(self, ask: _Ask) -> str:
        argv = [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            _POWERSHELL,
        ]
        return _run_dialog(argv, ask, self._env)


class CommandPrompt:
    """Runs a user-configured command whose stdout is the master password.

    For headless machines, or to read the password from the OS keychain
    (`security find-generic-password -w ...`, `secret-tool lookup ...`).
    """

    def __init__(self, command: str, env: Mapping[str, str]) -> None:
        """Parse the command once; it is never run through a shell.

        POSIX: split like a shell would. Windows: CreateProcess parses the command line
        itself, with Windows quoting rules, so it is passed through unchanged.
        """
        self._command: str | list[str] = command if os.name == "nt" else shlex.split(command)
        self._env = env

    def ask(self, account: str) -> str:
        """Run the command and return its stdout as the password."""
        if not self._command:
            raise UnlockError("--password-command is empty.")
        try:
            return _run_dialog(self._command, _unlock(account), self._env)
        except FileNotFoundError as e:
            raise UnlockError(f"--password-command not found: {self._command}") from e


def native_prompt(platform: str, env: Mapping[str, str]) -> MacDialog | LinuxDialog | WindowsDialog:
    """The dialog for this OS (`sys.platform` value)."""
    if platform == "darwin":
        return MacDialog(env)
    if platform == "win32":
        return WindowsDialog(env)
    return LinuxDialog(env)
