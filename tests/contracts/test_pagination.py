import pytest
from platform_contracts.pagination import decode_cursor, encode_cursor


def test_cursor_round_trip_is_bound_to_scope_and_arity():
    cursor = encode_cursor("data:assets:project-a", ("asset-1", "version-2"))

    assert decode_cursor(cursor, "data:assets:project-a", 2) == ("asset-1", "version-2")
    with pytest.raises(ValueError, match="Invalid list cursor"):
        decode_cursor(cursor, "data:assets:project-b", 2)
    with pytest.raises(ValueError, match="Invalid list cursor"):
        decode_cursor(cursor, "data:uploads:project-a", 1)


@pytest.mark.parametrize("cursor", ["not-base64!", "e30=", "", "AA=="])
def test_cursor_rejects_malformed_payloads(cursor):
    with pytest.raises(ValueError, match="Invalid list cursor"):
        decode_cursor(cursor, "data:assets:project-a", 2)
