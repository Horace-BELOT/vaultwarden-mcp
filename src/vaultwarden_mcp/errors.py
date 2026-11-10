"""Errors raised by vaultwarden-mcp.

Every error is a `VaultwardenMcpError`: the MCP layer turns it into a tool error the model
can read, so messages say what to do next. None of them ever carries a secret value.
"""


class VaultwardenMcpError(Exception):
    """Base class for every error this server reports to the client."""


class BitwardenCliError(VaultwardenMcpError):
    """The `bw` CLI is missing or exited with an error."""

    def __init__(self, command: str, message: str) -> None:
        """Keep the bw subcommand so the message points at the failing step."""
        super().__init__(f"bw {command} failed: {message}")
        self.command = command


class NotLoggedInError(VaultwardenMcpError):
    """The CLI has no account: the user must run `bw login` once, in a terminal."""


class UnlockError(VaultwardenMcpError):
    """The vault could not be unlocked (dialog cancelled, wrong password, no dialog)."""


class FieldNotFoundError(VaultwardenMcpError):
    """The requested field is absent or empty on the item."""


class DestinationError(VaultwardenMcpError):
    """The destination file is refused (relative path, missing folder, tracked by git...)."""
