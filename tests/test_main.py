import io
import json

from vaultwarden_mcp.__main__ import main


def test_main_exits_2_with_a_log_line_when_bw_is_missing() -> None:
    err = io.StringIO()
    assert main([], env={"PATH": ""}, err=err) == 2
    event = json.loads(err.getvalue())
    assert event["event"] == "startup.failed"
    assert "bitwarden-cli" in event["reason"]
