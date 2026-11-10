"""In-memory fakes of the ports, and a small vault fixture.

The fake CLI speaks the same JSON as `bw`, so the real pydantic models parse it.
"""

import io
import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime

from vaultwarden_mcp.errors import BitwardenCliError, UnlockError
from vaultwarden_mcp.logs import Log
from vaultwarden_mcp.vault import Vault, VaultSettings

MASTER_PASSWORD = "fake-master-password"
SESSION_KEY = "fake-session-key"

GITHUB_PASSWORD = "gh-pass-value-91c2"
GITHUB_TOKEN = "gh-token-value-4d7e"
STRIPE_SECRET = "stripe-secret-value-0b5a"
NOTE_SECRET = "note-only-secret-value-6f1d"

FOLDERS: list[dict[str, str | None]] = [
    {"id": "f-dev", "name": "Développement"},
    {"id": None, "name": "No Folder"},
]
COLLECTIONS: list[dict[str, str]] = [{"id": "c-infra", "name": "Infra Acme"}]

ITEMS: list[dict[str, object]] = [
    {
        "id": "i-github",
        "type": 1,
        "name": "GitHub",
        "notes": "Compte de déploiement",
        "folderId": "f-dev",
        "collectionIds": [],
        "login": {
            "username": "deploy-bot",
            "password": GITHUB_PASSWORD,
            "totp": "otpauth://totp/fake",
            "uris": [{"match": None, "uri": "https://github.com"}],
        },
        "fields": [{"name": "API token", "value": GITHUB_TOKEN, "type": 1}],
        "revisionDate": "2026-11-01T10:00:00.000Z",
    },
    {
        "id": "i-stripe",
        "type": 1,
        "name": "Stripe production",
        "notes": None,
        "folderId": None,
        "collectionIds": ["c-infra"],
        "login": {"username": "ops@example.com", "password": STRIPE_SECRET, "uris": None},
        "fields": None,
        "revisionDate": "2026-10-01T10:00:00.000Z",
    },
    {
        "id": "i-note",
        "type": 2,
        "name": "Clé SSH serveur",
        "notes": f"passphrase: {NOTE_SECRET}",
        "folderId": "f-dev",
        "secureNote": {"type": 0},
        "revisionDate": "2026-09-01T10:00:00.000Z",
    },
]

ALL_SECRETS = (GITHUB_PASSWORD, GITHUB_TOKEN, STRIPE_SECRET, NOTE_SECRET)


@dataclass
class FakeBw:
    """Fake `bw`: one account, one valid master password, one session key."""

    status: str = "locked"
    items: list[dict[str, object]] = field(default_factory=lambda: list(ITEMS))
    calls: list[tuple[str, ...]] = field(default_factory=list)
    _session_valid: bool = False

    def run(  # noqa: PLR0911 -- a fake CLI answers one subcommand per branch
        self, args: Sequence[str], *, session: str | None = None
    ) -> str:
        """Answer the subcommands the vault uses."""
        self.calls.append(tuple(args))
        if args[0] == "status":
            unlocked = self._session_valid and session == SESSION_KEY
            return json.dumps(
                {
                    "serverUrl": "https://vault.example.test",
                    "userEmail": None if self.status == "unauthenticated" else "me@example.test",
                    "lastSync": "2026-11-10T09:00:00.000Z",
                    "status": "unlocked" if unlocked else self.status,
                }
            )
        if not (self._session_valid and session == SESSION_KEY):
            raise BitwardenCliError(args[0], "Vault is locked.")
        match list(args):
            case ["list", "items"]:
                return json.dumps(self.items)
            case ["list", "folders"]:
                return json.dumps(FOLDERS)
            case ["list", "collections"]:
                return json.dumps(COLLECTIONS)
            case ["get", "item", item_id]:
                for item in self.items:
                    if item["id"] == item_id:
                        return json.dumps(item)
                raise BitwardenCliError("get", "Not found.")
            case ["get", "totp", _]:
                return "123456\n"
            case ["sync"]:
                return "Syncing complete."
            case ["lock"]:
                self._session_valid = False
                return "Your vault is locked."
        raise AssertionError(f"unexpected bw call: {args}")

    def unlock(self, password: str) -> str:
        """Accept only the fake master password."""
        self.calls.append(("unlock",))
        if password != MASTER_PASSWORD:
            raise BitwardenCliError("unlock", "Invalid master password.")
        self._session_valid = True
        return SESSION_KEY

    def count(self, *args: str) -> int:
        """How many times a subcommand was called."""
        return sum(1 for c in self.calls if c[: len(args)] == args)


@dataclass
class FakePrompt:
    """Returns a fixed answer, or cancels, and counts how often it was asked."""

    answer: str | None = MASTER_PASSWORD
    asked: list[str] = field(default_factory=list)

    def ask(self, account: str) -> str:
        """Record the account shown and answer."""
        self.asked.append(account)
        if self.answer is None:
            raise UnlockError("Unlock cancelled by the user.")
        return self.answer


@dataclass
class FakeClock:
    """Monotonic clock moved by hand."""

    now: float = 1000.0

    def __call__(self) -> float:
        """Current time."""
        return self.now

    def advance(self, seconds: float) -> None:
        """Move time forward."""
        self.now += seconds


@dataclass
class Harness:
    """A vault wired to fakes, with its log stream."""

    bw: FakeBw
    prompt: FakePrompt
    clock: FakeClock
    stream: io.StringIO
    log: Log
    vault: Vault

    def log_events(self) -> list[dict[str, object]]:
        """Parsed log lines."""
        return [json.loads(line) for line in self.stream.getvalue().splitlines()]


def harness(
    *,
    bw: FakeBw | None = None,
    prompt: FakePrompt | None = None,
    idle_lock_seconds: float = 3600,
    cache_seconds: float = 300,
) -> Harness:
    """Build a vault over fakes."""
    bw = FakeBw() if bw is None else bw
    prompt = FakePrompt() if prompt is None else prompt
    clock = FakeClock()
    stream = io.StringIO()
    log = Log(stream, lambda: datetime(2026, 11, 10, 12, 0, tzinfo=UTC))
    vault = Vault(
        bw=bw,
        prompt=prompt,
        clock=clock,
        settings=VaultSettings(idle_lock_seconds=idle_lock_seconds, cache_seconds=cache_seconds),
        log=log,
    )
    return Harness(bw=bw, prompt=prompt, clock=clock, stream=stream, log=log, vault=vault)
