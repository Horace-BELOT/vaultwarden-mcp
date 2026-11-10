import json
from dataclasses import asdict

import pytest

from tests.fakes import ALL_SECRETS, COLLECTIONS, FOLDERS, GITHUB_TOKEN, ITEMS
from vaultwarden_mcp.bitwarden import ITEMS as ITEMS_ADAPTER
from vaultwarden_mcp.bitwarden import BwItem
from vaultwarden_mcp.catalog import available_fields, extract_field, search, summarize
from vaultwarden_mcp.errors import FieldNotFoundError

PARSED = ITEMS_ADAPTER.validate_json(json.dumps(ITEMS))
FOLDER_NAMES = {str(f["id"]): str(f["name"]) for f in FOLDERS if f["id"]}
COLLECTION_NAMES = {str(c["id"]): str(c["name"]) for c in COLLECTIONS}


def _item(item_id: str) -> BwItem:
    return next(i for i in PARSED if i.id == item_id)


def _names(query: str, *, search_notes: bool = True) -> list[str]:
    found = search(PARSED, query, FOLDER_NAMES, COLLECTION_NAMES, search_notes=search_notes)
    return [s.name for s in found]


def test_summaries_never_contain_secret_values() -> None:
    dumped = json.dumps(
        [asdict(summarize(i, FOLDER_NAMES, COLLECTION_NAMES)) for i in PARSED],
        ensure_ascii=False,
    )
    for secret in ALL_SECRETS:
        assert secret not in dumped


def test_search_requires_every_word_to_match_some_attribute() -> None:
    assert _names("github deploy") == ["GitHub"]
    assert _names("github stripe") == []


def test_search_ignores_case_and_accents() -> None:
    assert _names("DEVELOPPEMENT") == ["Clé SSH serveur", "GitHub"]
    assert _names("cle ssh") == ["Clé SSH serveur"]


def test_search_matches_collection_uri_and_custom_field_name() -> None:
    assert _names("acme") == ["Stripe production"]
    assert _names("github.com") == ["GitHub"]
    assert _names("api token") == ["GitHub"]


def test_search_reports_a_note_match_without_its_content() -> None:
    found = search(PARSED, "passphrase", FOLDER_NAMES, COLLECTION_NAMES, search_notes=True)
    assert [s.name for s in found] == ["Clé SSH serveur"]
    assert found[0].matched_in == ("notes",)
    assert "passphrase" not in json.dumps(asdict(found[0]))


def test_search_skips_notes_when_disabled() -> None:
    assert _names("passphrase", search_notes=False) == []


def test_empty_query_lists_every_item_sorted_by_name() -> None:
    assert _names("") == ["Clé SSH serveur", "GitHub", "Stripe production"]


def test_available_fields_lists_only_non_empty_fields() -> None:
    assert available_fields(_item("i-github")) == (
        "username",
        "password",
        "totp",
        "notes",
        "field:API token",
    )
    assert available_fields(_item("i-note")) == ("notes",)


def test_extract_field_reads_a_custom_field_by_name() -> None:
    assert extract_field(_item("i-github"), "field:API token") == GITHUB_TOKEN


def test_extract_field_on_a_missing_field_lists_the_available_ones() -> None:
    with pytest.raises(FieldNotFoundError, match=r"Available: username, password\.$"):
        extract_field(_item("i-stripe"), "notes")
