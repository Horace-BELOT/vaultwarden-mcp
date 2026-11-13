"""Server tests through a real MCP client session, in memory."""

import json
from pathlib import Path

import pytest
from mcp.shared.memory import create_connected_server_and_client_session
from mcp.types import CallToolResult, TextContent

from tests.fakes import ALL_SECRETS, GITHUB_PASSWORD, Harness, harness
from vaultwarden_mcp.server import build_server

pytestmark = pytest.mark.anyio


def _never_tracked(_: Path) -> bool:
    return False


async def _call(h: Harness, tool: str, arguments: dict[str, object]) -> CallToolResult:
    server = build_server(
        h.vault, log=h.log, search_notes=True, idle_lock_minutes=60, git_unignored=_never_tracked
    )
    async with create_connected_server_and_client_session(server) as client:
        return await client.call_tool(tool, arguments)


def _wire(result: CallToolResult) -> str:
    """Everything the model would receive, as one string."""
    return result.model_dump_json()


def _text(result: CallToolResult) -> str:
    return " ".join(c.text for c in result.content if isinstance(c, TextContent))


async def test_only_get_secret_to_file_requires_user_interaction() -> None:
    h = harness()
    server = build_server(h.vault, log=h.log, search_notes=True, idle_lock_minutes=60)
    async with create_connected_server_and_client_session(server) as client:
        tools = (await client.list_tools()).tools
    flagged = {t.name for t in tools if (t.meta or {}).get("anthropic/requiresUserInteraction")}
    assert flagged == {"get_secret_to_file"}
    assert {t.name for t in tools} == {
        "vault_status",
        "search_secrets",
        "get_secret_to_file",
        "list_folders_and_collections",
        "sync_vault",
        "lock_vault",
    }


async def test_search_returns_metadata_and_no_secret_value() -> None:
    result = await _call(harness(), "search_secrets", {"query": ""})
    assert not result.isError
    assert result.structuredContent is not None
    assert result.structuredContent["total"] == 3
    for secret in ALL_SECRETS:
        assert secret not in _wire(result)


async def test_get_secret_writes_the_value_but_never_returns_or_logs_it(tmp_path: Path) -> None:
    h = harness()
    target = tmp_path / ".env"
    result = await _call(
        h,
        "get_secret_to_file",
        {
            "item_id": "i-github",
            "field": "password",
            "destination": str(target),
            "env_key": "GITHUB_PASSWORD",
            "reason": "run the deploy script",
        },
    )
    assert not result.isError, _text(result)
    assert target.read_text() == f"GITHUB_PASSWORD='{GITHUB_PASSWORD}'\n"
    assert GITHUB_PASSWORD not in _wire(result)
    assert GITHUB_PASSWORD not in h.stream.getvalue()
    written = h.log_events()[-1]
    assert (written["event"], written["reason"]) == ("secret.written", "run the deploy script")


async def test_get_secret_writes_the_current_totp_code(tmp_path: Path) -> None:
    target = tmp_path / "otp"
    result = await _call(
        harness(),
        "get_secret_to_file",
        {"item_id": "i-github", "field": "totp", "destination": str(target), "reason": "login"},
    )
    assert not result.isError, _text(result)
    assert target.read_text() == "123456"


async def test_a_refused_destination_fails_before_any_unlock(tmp_path: Path) -> None:
    h = harness()
    result = await _call(
        h,
        "get_secret_to_file",
        {
            "item_id": "i-github",
            "field": "password",
            "destination": "relative.env",
            "reason": "test",
        },
    )
    assert result.isError
    assert "absolute path" in _text(result)
    assert h.prompt.asked == []


async def test_an_unknown_field_is_a_tool_error_listing_available_fields(tmp_path: Path) -> None:
    result = await _call(
        harness(),
        "get_secret_to_file",
        {
            "item_id": "i-stripe",
            "field": "notes",
            "destination": str(tmp_path / "x"),
            "reason": "test",
        },
    )
    assert result.isError
    assert "Available: username, password." in _text(result)
    assert not (tmp_path / "x").exists()


async def test_status_reports_the_lock_state_without_prompting() -> None:
    h = harness()
    result = await _call(h, "vault_status", {})
    assert result.structuredContent is not None
    assert json.loads(json.dumps(result.structuredContent))["unlocked_here"] is False
    assert h.prompt.asked == []
