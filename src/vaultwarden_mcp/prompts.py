"""Master password prompts. Each satisfies `vaultwarden_mcp.vault.PasswordPrompt`.

The password goes from a native dialog (or a command the user configured) straight to this
process, then to `bw` through a one-shot environment variable: it never enters the MCP
conversation. The dialog text is passed through the environment too, so no quoting of the
account name into AppleScript or PowerShell is needed.
"""

import os
import shlex
import shutil
import subprocess
from collections.abc import Mapping, Sequence

from vaultwarden_mcp.errors import UnlockError

_TEXT_ENV = "VAULTWARDEN_MCP_PROMPT"
_TITLE = "Vaultwarden"

_APPLESCRIPT = """
set msg to system attribute "VAULTWARDEN_MCP_PROMPT"
set answer to display dialog msg default answer "" with hidden answer ¬
    buttons {"Cancel", "Unlock"} default button "Unlock" cancel button "Cancel" ¬
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
$ok = New-Object Windows.Forms.Button; $ok.Text = 'Unlock'; $ok.DialogResult = 'OK'
$ok.SetBounds(246, 110, 80, 28)
$no = New-Object Windows.Forms.Button; $no.Text = 'Cancel'; $no.DialogResult = 'Cancel'
$no.SetBounds(332, 110, 80, 28)
$f.AcceptButton = $ok; $f.CancelButton = $no; $f.Controls.AddRange(@($l, $t, $ok, $no))
if ($f.ShowDialog() -eq 'OK') { [Console]::Out.Write($t.Text); exit 0 } else { exit 1 }
"""


def _message(account: str) -> str:
    return (
        f"vaultwarden-mcp wants to unlock the vault of {account or 'this account'}.\n\n"
        "The master password goes to the Bitwarden CLI only, never to the AI assistant."
    )


def _run_dialog(command: str | Sequence[str], account: str, env: Mapping[str, str]) -> str:
    """Run a dialog command; exit 0 + stdout = password, any other exit = cancelled."""
    proc = subprocess.run(
        command if isinstance(command, str) else list(command),
        capture_output=True,
        text=True,
        env={**env, _TEXT_ENV: _message(account)},
        check=False,
    )
    if proc.returncode != 0:
        detail = proc.stderr.strip().splitlines()
        if detail and "-128" not in detail[-1]:  # -128 is AppleScript's "user cancelled"
            raise UnlockError(f"Password dialog failed: {detail[-1]}")
        raise UnlockError("Unlock cancelled by the user.")
    password = proc.stdout.removesuffix("\n").removesuffix("\r")
    if not password:
        raise UnlockError("Empty master password.")
    return password


class MacDialog:
    """`osascript` dialog with a masked field."""

    def __init__(self, env: Mapping[str, str]) -> None:
        """Keep the environment the dialog process inherits."""
        self._env = env

    def ask(self, account: str) -> str:
        """Show the dialog and return the typed password."""
        return _run_dialog(["osascript", "-e", _APPLESCRIPT], account, self._env)


class LinuxDialog:
    """`zenity --password`, or `kdialog --password` on KDE."""

    def __init__(self, env: Mapping[str, str]) -> None:
        """Keep the environment the dialog process inherits."""
        self._env = env

    def ask(self, account: str) -> str:
        """Show the dialog and return the typed password."""
        if not (self._env.get("DISPLAY") or self._env.get("WAYLAND_DISPLAY")):
            raise UnlockError(
                "No graphical session (DISPLAY/WAYLAND_DISPLAY unset): "
                "use --password-command instead (see README)."
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
            raise UnlockError(
                "Neither zenity nor kdialog is installed: install one "
                "(e.g. `sudo apt install zenity`) or use --password-command."
            )
        return _run_dialog(argv, account, self._env)


class WindowsDialog:
    """WinForms dialog through Windows PowerShell."""

    def __init__(self, env: Mapping[str, str]) -> None:
        """Keep the environment the dialog process inherits."""
        self._env = env

    def ask(self, account: str) -> str:
        """Show the dialog and return the typed password."""
        argv = [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            _POWERSHELL,
        ]
        return _run_dialog(argv, account, self._env)


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
            return _run_dialog(self._command, account, self._env)
        except FileNotFoundError as e:
            raise UnlockError(f"--password-command not found: {self._command}") from e


def native_prompt(platform: str, env: Mapping[str, str]) -> MacDialog | LinuxDialog | WindowsDialog:
    """The dialog for this OS (`sys.platform` value)."""
    if platform == "darwin":
        return MacDialog(env)
    if platform == "win32":
        return WindowsDialog(env)
    return LinuxDialog(env)
