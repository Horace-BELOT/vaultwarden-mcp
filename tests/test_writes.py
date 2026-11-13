"""create_secret / update_secret through a real MCP client session, in memory."""

import base64
import json
from pathlib import Path
from typing import Any

import pytest
from mcp.shared.memory import create_connected_server_and_client_session
from mcp.types import CallToolResult, TextContent

from tests.fakes import (
    ALL_SECRETS,
    GITHUB_PASSWORD,
    FakeValuePrompt,
    Harness,
    harness,
)
from vaultwarden_mcp.server import build_server

pytestmark = pytest.mark.anyio

WRITE_TOOLS = {"create_secret", "update_secret"}


def _never_tracked(_: Path) -> bool:
    return False


async def _call(
    h: Harness,
    tool: str,
    arguments: dict[str, object],
    *,
    prompt: FakeValuePrompt | None = None,
) -> CallToolResult:
    server = build_server(
        h.vault,
        log=h.log,
        search_notes=True,
        idle_lock_minutes=60,
        git_unignored=_never_tracked,
        allow_writes=True,
        value_prompt=prompt,
    )
    async with create_connected_server_and_client_session(server) as client:
        return await client.call_tool(tool, arguments)


def _text(result: CallToolResult) -> str:
    return " ".join(c.text for c in result.content if isinstance(c, TextContent))


def _out(result: CallToolResult) -> dict[str, Any]:
    assert not result.isError, _text(result)
    assert result.structuredContent is not None
    return result.structuredContent


def _item(h: Harness, item_id: str) -> dict[str, Any]:
    found: dict[str, Any] = next(i for i in h.bw.items if i["id"] == item_id)
    return found


def _no_leak(h: Harness, result: CallToolResult, *values: str) -> None:
    """Neither the result, the log, nor any bw argument carries the values."""
    for value in (*values, *ALL_SECRETS):
        assert value not in result.model_dump_json()
        assert value not in h.stream.getvalue()
        assert not any(value in " ".join(c) for c in h.bw.calls)


async def test_write_tools_exist_only_with_allow_writes() -> None:
    h = harness()
    for allow, expected in ((False, set()), (True, WRITE_TOOLS)):
        server = build_server(
            h.vault, log=h.log, search_notes=True, idle_lock_minutes=60, allow_writes=allow
        )
        async with create_connected_server_and_client_session(server) as client:
            tools = (await client.list_tools()).tools
        names = {t.name for t in tools}
        flagged = {t.name for t in tools if (t.meta or {}).get("anthropic/requiresUserInteraction")}
        assert names & WRITE_TOOLS == expected
        assert flagged == {"get_secret_to_file", *expected}


async def test_create_generates_a_password_in_the_named_collection(tmp_path: Path) -> None:
    h = harness()
    env = tmp_path / ".env"
    result = await _call(
        h,
        "create_secret",
        {
            "name": "Postgres staging",
            "reason": "new database",
            "source": "generate",
            "username": "app",
            "uris": ["postgres://db.example.test"],
            "collections": ["infra ACME"],
            "destination": str(env),
            "env_key": "DB_PASSWORD",
        },
    )
    out = _out(result)
    assert (out["collections"], out["length"], out["source"]) == (["Infra Acme"], 32, "generate")
    item = _item(h, out["item_id"])
    assert (item["organizationId"], item["collectionIds"]) == ("o-acme", ["c-infra"])
    assert (item["type"], item["login"]["username"]) == (1, "app")
    password = item["login"]["password"]
    assert len(password) == 32
    assert env.read_text() == f"DB_PASSWORD='{password}'\n"
    _no_leak(h, result, password)
    assert h.log_events()[-1]["event"] == "secret.created"


async def test_create_takes_the_value_from_the_dialog() -> None:
    h, prompt = harness(), FakeValuePrompt()
    out = _out(
        await _call(
            h,
            "create_secret",
            {
                "name": "Sentry DSN",
                "reason": "r",
                "source": "dialog",
                "item_type": "secure_note",
                "field": "field:dsn",
            },
            prompt=prompt,
        )
    )
    item = _item(h, out["item_id"])
    assert item["type"] == 2
    assert item["fields"] == [
        {"name": "dsn", "value": "typed-value-77aa", "type": 1, "linkedId": None}
    ]
    assert "Sentry DSN" in prompt.messages[0]
    assert "personal vault" in prompt.messages[0]


async def test_create_reads_a_file_and_drops_one_trailing_newline(tmp_path: Path) -> None:
    h = harness()
    source = tmp_path / "token"
    source.write_text("cli-token-5e5e\n")
    out = _out(
        await _call(
            h,
            "create_secret",
            {"name": "CLI token", "reason": "r", "source": "file", "source_path": str(source)},
        )
    )
    assert _item(h, out["item_id"])["login"]["password"] == "cli-token-5e5e"


async def test_a_multiline_file_is_only_accepted_for_notes(tmp_path: Path) -> None:
    h = harness()
    source = tmp_path / "env"
    source.write_text("A=1\nB=2\n")
    refused = await _call(
        h,
        "create_secret",
        {"name": "x", "reason": "r", "source": "file", "source_path": str(source)},
    )
    assert refused.isError
    assert "several lines" in _text(refused)
    out = _out(
        await _call(
            h,
            "create_secret",
            {
                "name": ".env app",
                "reason": "r",
                "source": "file",
                "source_path": str(source),
                "item_type": "secure_note",
                "field": "notes",
            },
        )
    )
    assert _item(h, out["item_id"])["notes"] == "A=1\nB=2\n"


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ({"name": "Stripe production", "collections": ["Infra Acme"]}, "use update_secret"),
        ({"name": "x", "collections": ["Infra Acme", "Partner"]}, "different organizations"),
        ({"name": "x", "collections": ["Nope"]}, "Partner (Partner Inc)"),
        ({"name": "x", "folder": "Nope"}, "Folders: Développement"),
        ({"name": "x", "item_type": "secure_note"}, "Only login items have a password"),
    ],
)
async def test_create_refuses_bad_targets_without_creating(
    arguments: dict[str, object], message: str
) -> None:
    h = harness()
    result = await _call(h, "create_secret", {"reason": "r", "source": "generate", **arguments})
    assert result.isError
    assert message in _text(result)
    assert h.bw.count("create") == 0


async def test_a_refused_destination_fails_before_any_unlock() -> None:
    h = harness()
    result = await _call(
        h,
        "create_secret",
        {"name": "x", "reason": "r", "source": "generate", "destination": "rel/.env"},
    )
    assert result.isError
    assert h.prompt.asked == []


async def test_dialog_without_a_window_or_cancelled_creates_nothing() -> None:
    for prompt in (None, FakeValuePrompt(answer=None)):
        h = harness()
        args: dict[str, object] = {"name": "x", "reason": "r", "source": "dialog"}
        result = await _call(h, "create_secret", args, prompt=prompt)
        assert result.isError
        assert h.bw.count("create") == 0


async def test_update_password_keeps_the_previous_one_in_history(tmp_path: Path) -> None:
    h = harness()
    out = _out(
        await _call(
            h,
            "update_secret",
            {
                "item_id": "i-github",
                "field": "password",
                "reason": "rotation",
                "source": "generate",
                "length": 40,
                "special_chars": True,
            },
        )
    )
    item = _item(h, "i-github")
    assert len(item["login"]["password"]) == 40
    assert item["passwordHistory"][0]["password"] == GITHUB_PASSWORD
    assert item["fields"][0]["name"] == "API token"  # the rest of the item is kept
    assert out["previous_value_in_history"] is True
    stdin = json.loads(base64.b64decode(h.bw.stdins[-1]))
    assert stdin["id"] == "i-github"


async def test_update_notes_says_the_previous_value_is_gone(tmp_path: Path) -> None:
    h = harness()
    source = tmp_path / "n"
    source.write_text("new note\n")
    out = _out(
        await _call(
            h,
            "update_secret",
            {
                "item_id": "i-note",
                "field": "notes",
                "reason": "r",
                "source": "file",
                "source_path": str(source),
            },
        )
    )
    assert out["previous_value_in_history"] is False
    assert "no history" in out["note"]
    assert _item(h, "i-note")["notes"] == "new note\n"


async def test_update_refuses_a_password_on_a_secure_note() -> None:
    h = harness()
    result = await _call(
        h,
        "update_secret",
        {"item_id": "i-note", "field": "password", "reason": "r", "source": "generate"},
    )
    assert result.isError
    assert h.bw.count("edit") == 0


async def test_list_folders_and_collections_names_the_organizations() -> None:
    out = _out(await _call(harness(), "list_folders_and_collections", {}))
    assert out["folders"] == ["Développement"]  # "No Folder" has no id: not a target
    assert {"name": "Partner", "organization": "Partner Inc"} in out["collections"]
