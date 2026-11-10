"""Integration with the real `bw`, in a throwaway data folder (never the user's login).

Skipped when `bw` is not installed; CI installs it on Linux, macOS and Windows.
"""

import os
from pathlib import Path

import pytest

from vaultwarden_mcp.bitwarden import BitwardenCli, BwStatus, find_bw

BW = find_bw(None, os.environ.get("PATH"))

pytestmark = pytest.mark.skipif(BW is None, reason="Bitwarden CLI (bw) is not installed")


def _cli(tmp_path: Path) -> BitwardenCli:
    assert BW is not None
    return BitwardenCli(BW, {**os.environ, "BITWARDENCLI_APPDATA_DIR": str(tmp_path)})


def test_real_bw_status_parses_on_a_fresh_data_folder(tmp_path: Path) -> None:
    status = BwStatus.model_validate_json(_cli(tmp_path).run(["status"]))
    assert status.status == "unauthenticated"


def test_real_bw_accepts_a_self_hosted_server_url(tmp_path: Path) -> None:
    cli = _cli(tmp_path)
    cli.run(["config", "server", "https://vault.example.test"])
    status = BwStatus.model_validate_json(cli.run(["status"]))
    assert status.server_url == "https://vault.example.test"
