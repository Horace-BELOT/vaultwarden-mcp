import pytest

from tests.fakes import FakeBw, FakePrompt, harness
from vaultwarden_mcp.errors import NotLoggedInError, UnlockError


def test_first_use_asks_once_and_reuses_the_session() -> None:
    h = harness()
    h.vault.snapshot()
    h.vault.item("i-github")
    assert h.prompt.asked == ["me@example.test"]
    assert h.bw.count("unlock") == 1
    assert h.vault.unlocked


def test_status_never_prompts() -> None:
    h = harness()
    assert h.vault.status().status == "locked"
    assert h.prompt.asked == []


def test_a_logged_out_cli_raises_without_prompting() -> None:
    h = harness(bw=FakeBw(status="unauthenticated"))
    with pytest.raises(NotLoggedInError, match="bw login"):
        h.vault.snapshot()
    assert h.prompt.asked == []


def test_a_wrong_master_password_raises_unlock_error() -> None:
    h = harness(prompt=FakePrompt(answer="wrong"))
    with pytest.raises(UnlockError, match="wrong master password"):
        h.vault.snapshot()
    assert not h.vault.unlocked


def test_a_cancelled_dialog_raises_unlock_error() -> None:
    h = harness(prompt=FakePrompt(answer=None))
    with pytest.raises(UnlockError, match="cancelled"):
        h.vault.snapshot()


def test_idle_session_is_locked_and_asks_again() -> None:
    h = harness(idle_lock_seconds=60)
    h.vault.snapshot()
    h.clock.advance(61)
    h.vault.snapshot()
    assert len(h.prompt.asked) == 2
    assert [e["event"] for e in h.log_events()] == [
        "vault.unlocked",
        "vault.locked",
        "vault.unlocked",
    ]


def test_item_list_is_cached_within_the_cache_window() -> None:
    h = harness(cache_seconds=300)
    h.vault.snapshot()
    h.clock.advance(100)
    h.vault.snapshot()
    assert h.bw.count("list", "items") == 1
    h.clock.advance(300)
    h.vault.snapshot()
    assert h.bw.count("list", "items") == 2


def test_lock_forgets_the_session_and_logs_why() -> None:
    h = harness()
    h.vault.snapshot()
    h.vault.lock("tool")
    assert not h.vault.unlocked
    assert h.bw.count("lock") == 1
    assert h.log_events()[-1] == {
        "ts": "2026-11-10T12:00:00+00:00",
        "event": "vault.locked",
        "reason": "tool",
    }


def test_lock_when_already_locked_does_nothing() -> None:
    h = harness()
    h.vault.lock("tool")
    assert h.bw.calls == []
