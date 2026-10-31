"""The two digest spellings have explicit, non-interchangeable contracts."""

from platform_core.domain.canonical import canonical_json, digest_hex, digest_uri


def test_canonical_digest_spellings_share_bytes_but_not_identity_format():
    value = {"z": [2, 1], "a": "水"}

    assert canonical_json(value) == b'{"a":"\xe6\xb0\xb4","z":[2,1]}'
    assert len(digest_hex(value)) == 64
    assert digest_uri(value) == "sha256:" + digest_hex(value)
