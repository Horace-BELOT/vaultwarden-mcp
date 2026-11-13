import string

import pytest

from vaultwarden_mcp.editor import ValueRequest, check_request, generate, keeps_history, set_value
from vaultwarden_mcp.errors import ValueSourceError


@pytest.mark.parametrize("special", [False, True])
def test_generate_has_the_length_and_every_character_class(special: bool) -> None:
    for _ in range(50):
        value = generate(12, special_chars=special)
        assert len(value) == 12
        assert any(c in string.ascii_lowercase for c in value)
        assert any(c in string.ascii_uppercase for c in value)
        assert any(c in string.digits for c in value)
        assert any(not c.isalnum() for c in value) is special


@pytest.mark.parametrize(
    ("request_", "field", "message"),
    [
        (
            ValueRequest(source="generate", source_path=None, length=8, special_chars=False),
            "password",
            "between",
        ),
        (
            ValueRequest(source="file", source_path=None, length=32, special_chars=False),
            "password",
            "needs source_path",
        ),
        (
            ValueRequest(source="dialog", source_path="/x", length=32, special_chars=False),
            "password",
            "only used with",
        ),
        (
            ValueRequest(source="dialog", source_path=None, length=32, special_chars=False),
            "notes",
            'use source "file"',
        ),
        (
            ValueRequest(source="file", source_path="rel", length=32, special_chars=False),
            "password",
            "absolute",
        ),
    ],
)
def test_check_request_refuses_bad_requests(
    request_: ValueRequest, field: str, message: str
) -> None:
    with pytest.raises(ValueSourceError, match=message):
        check_request(request_, field)


def test_set_value_updates_a_field_in_place_and_does_not_touch_the_input() -> None:
    data = {"fields": [{"name": "k", "value": "old", "type": 0}]}
    out = set_value(data, "field:k", "new")
    assert out["fields"] == [{"name": "k", "value": "new", "type": 0}]
    assert data["fields"][0]["value"] == "old"
    assert keeps_history(data, "field:k") is False  # text field: Bitwarden keeps no history
    assert keeps_history(data, "field:new") is True  # created hidden
